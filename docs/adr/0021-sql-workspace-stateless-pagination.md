# ADR-0021: SQL Workspace 数据库限行与无状态续页

- 状态:Accepted；“连接与资源边界”一节被 **ADR-0023** 取代（2026-08-22）
- 日期:2026-08-12
- 范围:SQL Workspace 查询、JobBackend、ResultStore 与结果 UI
- 取代说明:ADR-0023（SQL 控制台会话式执行）取代本 ADR 否决长生命周期游标会话的结论，
  并逐项回答本文列出的八项缺失前提。本 ADR 的分页、数据库限行与无状态续页决策在
  **job 路径上继续有效**，由 ADR-0023 显式继承。

## 背景

原 SQL Workspace 的 `max_rows` 只在 worker 消费驱动结果时截断。数据库收到的仍是无界
SELECT，DM 等适配器还会按默认 1000 行 `fetchmany`，导致选择 100 行时仍可能让数据库执行
无界查询并向驱动传输多余数据。页面的“分页”也只是读取已物化的本地 Parquet，并不能在
第一页后继续获取数据库结果。

长期持有 cursor 的 query session 可以提供快照式翻页，但会绑定 worker、数据库连接和凭据
生命周期，还需要 worker affinity、租户配额、idle TTL、异常接管和 session reset。当前架构
没有这些安全边界。

## 决策

1. API 分离 `page_size` 与 `max_result_rows`。`max_rows` 仅作为旧客户端/旧 Job 兼容字段。
2. worker 使用 sqlglot AST 按 MySQL、PostgreSQL、Oracle 兼容语法（DM、DB2 使用其正式支持
   的 FETCH 子集）生成数据库窗口，不做字符串尾部拼接。每个页面最多向数据库请求
   `min(page_size, remaining_safety_rows) + 1` 行；多出的 1 行仅用于判断 `has_more`。
3. 第一批达到 `page_size` 即写入 ResultStore 并更新 ResultSet，API 可在 Job 终态前读取。
4. 续页采用架构内的无状态 Job：用户点击下一页时，API 复用原查询、绑定参数、数据源、
   JobBackend 和同一 ResultStore spool，入队一个新的 AST OFFSET/LIMIT 页面任务。已读页面
   继续从 spool 返回。
5. 无状态续页只对存在顶层 `ORDER BY` 的查询开放。每一页是数据源默认隔离级别下的一次
   新读取，不承诺跨页快照；UI 必须明确展示这一语义。没有顶层 `ORDER BY` 时保留第一页，
   但禁用下一页并提示用户添加排序，避免静默返回不稳定页。
6. 不执行自动 `COUNT(*)`。只要 N+1 证明存在后续行，`total_rows` 保持未知，UI 显示
   `loaded_rows+`。
7. Job 响应分别暴露 queue、connect、execute/first-row、fetch、spool 和 total 耗时。
   首屏在尚无结果时按 150ms 轮询，拿到第一页后退回 1s；保留普通 HTTP 轮询降级路径，
   本次不引入 SSE 服务。
8. API 预览二进制值只显示长度，超长字符串截到 4096 字符；spool 与导出数据不因此改写。

## 连接与资源边界

本方案不创建跨请求 query session，不在浏览器空闲时持有 cursor、连接或解密后的凭据。
每个页面 Job 完成、失败或取消时都由现有适配器 `finally` 关闭 cursor/连接；worker 异常时由
进程和现有 stale-job 回收路径清理。ResultStore 继续使用既有 TTL GC。确定性的页面 Job ID
使重复的同一 offset 请求幂等，不会并发追加同一页。

本次不增加连接池。当前五种适配器尚无统一、可验证的 session reset、凭据版本失效、坏连接
淘汰与租户/数据源隔离协议；在这些条件齐备前复用连接会扩大凭据更新和 session 污染风险。
连接池必须作为独立 ADR 与跨方言集成测试后续评估。

## 后果

- 首屏数据库工作量从“最多安全上限甚至无界”降为单页 N+1，驱动预取大小与请求页面一致。
- 有稳定排序的查询可以按需获取数据库后续页；上一页从本地 spool 稳定读取。
- 数据在两次页面请求之间变化时，OFFSET 页可能移动。该边界会在 UI 中显示；需要快照一致性
  的场景应改用一次性导出，未来可在具备隔离与资源治理后增加 keyset/session 模式。
- 旧 Job 没有分页元数据时保持原 eager spool 行为，避免升级后历史任务不可读。
