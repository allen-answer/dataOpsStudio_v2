# AI 元数据错误与目标表达式提示修复设计

## 背景与根因

SQL 工作台的 AI 生成接口必须先取得真实表结构。当前 DB2 数据源没有元数据缓存，
现场调用在 schema 探测阶段返回 `metadata_probe_failed`，该异常发生在 AI Gateway 调用
之前。前端未对该错误码做本地化，因而直接显示英文后端消息。允许 AI 在没有真实
schema 的情况下继续生成会引入表名、字段名幻觉，因此本修复保持失败关闭。

数据对比任务响应已经同时提供 `source_projection_details` 和
`target_projection_details`。对比列编辑器只在源列下使用了
`CompareExpressionLabel`，目标列输入框没有消费目标侧详情，导致自动别名
`RESULT_n` 无法查看目标 SQL 的完整列表达式。

## 方案

### AI 元数据失败

1. 后端在组装 AI schema 上下文时捕获结构化 `ApiError`，对
   `metadata_probe_failed` 写入一次失败审计后原样重新抛出，不记录连接参数、凭据或
   schema 内容。
2. 前端识别 `metadata_probe_failed`，显示本地化、可操作的中文提示：
   “无法读取数据源结构，请先测试数据源连接并刷新元数据后重试。”英文界面提供等价
   提示。
3. 不调用 AI Gateway，不使用空 schema，也不把数据库驱动的底层错误暴露给页面。

### 目标侧 `RESULT_n` 表达式

1. 新增目标侧详情查询函数：优先按目标列名匹配
   `target_projection_details`；对生成别名保留按投影位置兜底，以兼容列名编辑和旧任务。
2. 在目标列输入框下方复用 `CompareExpressionLabel`。仅当目标详情标记为
   `generated` 时显示紧凑的 `RESULT_n` 信息入口；点击后展示目标 SQL 的完整表达式。
3. 源列、普通列和旧响应行为保持不变。没有目标详情时不显示额外 UI，也不报错。

## 数据流与错误处理

- AI：用户提交自然语言 → 后端尝试取得缓存或探测元数据 → 探测失败时写失败审计并
  返回结构化 503 → 前端按错误码显示本地化指引。
- 对比：任务响应目标投影详情 → 目标列名/投影位置匹配 →
  `CompareExpressionLabel` → 点击查看原始表达式。

## 验证

1. 后端契约测试验证元数据探测失败仍返回 503、不会调用 AI Gateway，并产生脱敏失败
   审计。
2. 前端 Playwright 测试验证 AI 弹窗显示本地化指引而不是英文后端原文。
3. 前端对比测试验证目标 `RESULT_n` 信息入口可见、可点击并展示目标表达式；缺少
   `target_projection_details` 时页面正常。
4. 运行相关 pytest、ruff、mypy、前端 typecheck/build 和对应 Playwright 用例；提交后
   等待 PR 全部 CI 通过。

## 发布边界

只修改 AI 错误处理、对应文案、对比列编辑器和回归测试。不修改数据源凭据、元数据
缓存策略、AI egress 规则、SQL 执行逻辑或数据库 schema。合并后再构建前端并增量同步
到 D 盘运行目录。
