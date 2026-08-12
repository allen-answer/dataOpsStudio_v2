# ADR-0022: SQL Job 轻量进度、分组限流与执行可观测性

- 状态:Accepted
- 日期:2026-08-12
- 范围:SQL Workspace、Job API、worker、ResultStore、API 中间件与镜像版本

## 背景与根因

ADR-0021 已把页面大小、安全结果上限和数据库 N+1 限行分开，并以无状态有序 OFFSET Job
实现按需续页。剩余的长时间无结果和 429 不是单一数据库问题，而是状态读取链路的放大效应：

1. 第一批结果未就绪时，浏览器每 150ms 分别读取 Job 和 Result；单标签页理论峰值约为
   `2 * 60 / 0.15 = 800` 请求/分钟。第一批就绪后仍约为 120 请求/分钟，正好耗尽原全局
   IP 桶，多个控制台或标签页会更早互相影响。
2. Result GET 在无新数据时仍读取 manifest/Parquet 路径；通用中间件还为每次轮询写入开始、
   完成两条审计记录，形成 API 和 PostgreSQL 写放大。
3. 前端没有结果版本、AbortController、429 Retry-After、页面可见性或跨标签协调。同一个旧 Job
   在重新执行、切换控制台或网络超时后可能仍有定时器继续请求，产生“幽灵轮询”。
4. Job 的 `created_at -> finished_at` 只能表示端到端时间，无法区分排队、连接、数据库执行到
   首行、读取和落盘。聚合查询即使输出被 LIMIT 约束，也仍可能扫描大量输入；把总耗时显示为
   “数据库执行”或把输出限行称为“扫描下推”都会误导诊断。
5. API 与 worker 使用同一 Compose 镜像声明，但运行时没有提交号契约，无法从页面或接口证明
   两个进程实际运行的是预期构建。

## 决策

### 1. 轻量进度契约

新增 `GET /api/jobs/{job_id}/progress?after_version=N`。单次读取返回 Job 状态与 ResultStore
manifest 摘要，不读取 Parquet 行：

- `status`、`terminal`、`error`、`error_code`；
- `result_set_id`、`loaded_rows`、`result_version`、`columns_ready`、`first_batch_ready`；
- `has_new_result`（`result_version > after_version`）与服务端建议的 `retry_after_ms`；
- `truncated`、分页能力以及阶段计时和安全限行分析。

manifest 每次产生可观察变化时递增 `result_version`。旧 manifest 没有版本时按是否已有列/行推导
兼容版本，旧 Job 没有新增元数据时字段保持 `null`，不得因升级变成失败。

前端仅在 `has_new_result=true` 或首次拿到列/行时读取 Result API。Result API 继续负责本地 spool
分页；用户明确点击下一页时仍按 ADR-0021 创建数据库续页 Job。

### 2. 自适应、可取消的单一轮询循环

每个页面、每个活动 Job 最多有一个进度循环：

- 有新版本时回到 1 秒；无变化依次退避到 2、3、5 秒；
- 429 必须读取 `Retry-After`，网络错误采用有上限的指数退避；
- 页面不可见时至少使用 5 秒间隔；重新执行、切换控制台、卸载页面和 Job 终态时同时清理
  timer 与 AbortController；
- 同源标签页通过 `BroadcastChannel` 发布 Job 的最近版本与下一次建议时间；不支持该 API 时
  安全降级为各标签页自适应轮询。

不引入 SSE 常驻连接。本次问题的主要成本来自无变化读取和重复轮询，版本化长轮询契约已能在
现有 FastAPI、PostgreSQL JobBackend 和本地 ResultStore 架构内消除放大；未来可在同一进度
响应契约上增加 SSE，而不改变 UI 状态模型。

### 3. 按风险与读取成本分组限流

限流键优先使用已认证 `user_id`，匿名登录使用 IP；桶必须包含策略组，禁止一个查询页面耗尽
登录或其他用户的额度。默认滑动窗口为：

| 组 | 典型请求 | 默认额度 |
|---|---|---:|
| `auth` | 登录 | 10/分钟/IP |
| `sensitive_write` | 管理写操作 | 60/分钟/用户 |
| `sql_control` | 执行、续页、取消、导出 | 30/分钟/用户 |
| `job_read` | Job 进度与 Result 读取 | 300/分钟/用户 |
| `general_read` | 其他读取 | 120/分钟/用户 |

健康检查和版本接口不占业务桶。429 响应携带整数秒 `Retry-After`。按 5 秒稳态间隔，单控制台
10 分钟查询约 120 次进度读取；两个活动控制台约 240 次/10 分钟，任一 60 秒滑窗远低于
300。300 次/分钟以上的突发读取仍会受限；控制类请求不能借用读取额度。

进度和结果读取不再写通用“请求开始/完成”审计记录；SQL 执行、续页、取消、导出、登录及
授权等业务/安全事件继续审计。进度请求仍保留访问日志和指标，避免把高频观察行为写入业务
审计表。

### 4. 安全限行语义

继续使用 sqlglot AST 生成各正式支持方言的顶层输出窗口，绝不通过字符串尾部拼接：

- 保留用户更小的 `LIMIT/FETCH/TOP/ROWNUM` 等价上限；
- 对 `WITH`、`ORDER BY`、参数、末尾分号和子查询保持 AST 语义；
- 聚合、`DISTINCT`、窗口函数和集合运算只在最终输出施加 N+1 安全上限，不把 LIMIT 错误
  注入输入子查询；
- `limit_pushdown=true` 仅表示能合理减少源行扫描的简单查询。上述需要先完成聚合、去重、
  排序或集合运算的形状返回 `false` 和原因；`output_limit_applied` 独立保持为 `true`。

worker 仍以请求批量大小执行 `fetchmany`，并保留 spool 总行数/字节数、超时、取消和只读守卫。
不自动执行 `COUNT(*)`。总数未知且已确认后续行时继续显示 `N+`。

### 5. 阶段计时与诊断字段

进度响应只暴露时间、计数、散列和枚举，不包含完整 SQL、绑定参数、凭据或数据库地址：

- 时间点:`queued_at`、`claimed_at`、`connect_started_at`、`connected_at`、
  `execute_started_at`、`first_row_at`、`first_batch_at`、`finished_reading_at`、`finished_at`；
- 耗时:`queue_ms`、`connect_ms`、`execute_to_first_row_ms`、`fetch_ms`、`spool_ms`、`total_ms`；
- 计数/上下文:`rows_read`、`rows_returned`、`max_rows`、`limit_pushdown`、
  `output_limit_applied`、`query_shape`、`effective_sql_hash`、`db_type`、`worker_id`。

`first_row_at` 只有驱动实际返回至少一行时才能设置；空 batch 不得伪装首行。`first_batch_at`
表示第一批已原子写入 manifest，可被 UI 读取。聚合查询 UI 明确提示输出限行不等于减少数据库
扫描，并引导用户使用现有 EXPLAIN、表统计/索引元数据和数据库锁等待诊断；系统不自动建索引。

### 6. 构建版本一致性

镜像构建注入应用版本、Git commit 和 image version，写入 OCI label 与运行时环境。新增
`GET /api/version`，UI 显示短提交号；不暴露仓库路径或构建主机信息。Compose 中 API 与 worker
继续引用同一个 image，CI 同时校验：

1. `/api/version` 的 commit 等于当前构建提交；
2. API 与 worker 容器的 image digest/ID 相同。

这证明“构建内容一致”，不替代部署环境对实际运行提交、迁移状态和依赖服务健康的验证。

## 状态与降级

| Job 状态 | 结果状态 | UI 行为 |
|---|---|---|
| pending/running | 无版本变化 | 只轮询 progress，自适应退避 |
| running | 新版本/首批就绪 | 读取一次 Result，立即渲染并继续低频 progress |
| succeeded | 已有结果 | 最后读取一次新版本后停止 |
| failed/cancelled | 可能有已落盘页 | 展示终态错误；已缓存页仍可读 |
| 旧 Job/旧 manifest | 无新字段 | 使用兼容默认值和原 Result API |
| 429/网络错误 | 未知 | 遵守 Retry-After/指数退避，不创建第二循环 |

## 后果与未采用方案

- 进度读取从“每次 Job + Result”收敛为单个 manifest 摘要读取；首批显示仍不依赖 Job 终态。
- 分组额度解决不同风险请求互相挤占，但单进程内存限流不提供跨 API 副本的全局精确计数；
  多副本部署若需要强一致配额，应以独立 ADR 引入共享计数器。
- 未引入数据库 cursor session、worker affinity、自动 COUNT、自动索引或连接池，继续沿用
  ADR-0021 的无状态续页和连接生命周期，避免扩大总体架构与凭据/session 隔离风险。
- 未将“顶层输出 LIMIT”虚假描述为所有查询都减少数据库扫描；诊断字段允许在 UI 和日志中
  区分应用等待、数据库首行以及结果处理。
