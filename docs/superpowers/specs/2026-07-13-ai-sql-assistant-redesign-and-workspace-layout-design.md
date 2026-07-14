# AI SQL 助手重设计与 SQL 工作台布局修复

## 1. 背景

SQL 工作台当前以模态框提供“自然语言生成 SQL”。现场使用中出现两类问题：

1. AI 请求在约 2 秒后返回失败，页面只显示“AI 未能生成 SQL，请换种说法再试”，无法判断是鉴权、限流、超时、推理输出被截断、响应格式异常，还是 SQL 校验失败。
2. 工作台输入较长 SQL 时，Monaco 编辑器的最小内容宽度会撑开 AppShell 主内容区，导致页面整体出现数倍于视口的横向滚动，数据源和操作区被推到屏幕外。

本设计将 AI SQL 从一次性模态框改为可持续使用的右侧助手面板，同时补齐 Provider 响应归一化、自动推理策略、一次定向修复、SQL 安全校验和可操作诊断。布局问题采用独立的小范围约束修复，不重做整个工作台。

## 2. 已验证的根因

### 2.1 AI 失败信息丢失

当前链路如下：

1. `/api/datasources/{datasource_id}/ai/sql-generate` 读取真实表结构。
2. 路由以固定 `max_tokens=800` 调用 AI Gateway。
3. OpenAI-compatible Provider 只接受非空 `choices[0].message.content`。
4. HTTP 错误、缺少 choices、content 非字符串或空 content 最终都折叠为 `ProviderError`。
5. 路由只审计异常类名，前端再把所有 `ok=false` 或空 SQL 映射为同一条泛化提示。

现场失败审计只能证明请求已进入 Provider 阶段，不能再从现有记录确定具体 Provider 返回形态。因此本设计不把“推理 token 耗尽”写成唯一既定根因，而是把它作为需要明确识别的一类响应。

推理型兼容模型可能将推理过程放在独立的 `reasoning_content` 中，并让 `max_tokens` 同时限制推理与最终回答；`finish_reason=length` 则表示输出达到长度限制。固定 800 token 可能只留下推理内容而没有最终 SQL。参考：

- <https://api-docs.deepseek.com/api/create-chat-completion>
- <https://api-docs.deepseek.com/guides/thinking_mode>

### 2.2 页面横向溢出

真实浏览器测量显示视口宽度约为 1920px，而文档宽度被扩展到约 6300px。最外层 AppShell 的主内容 `<main class="flex-1 overflow-y-auto">` 没有 `min-w-0` 和横向边界，Monaco 的长行最小内容宽度沿 flex 布局向上传播，撑宽整个页面。

SQL 工作台内部虽然已有部分 `min-w-0`，但无法阻止外层 flex item 扩张。根因不是接口失败，也不是 JavaScript 运行错误。

## 3. 目标与非目标

### 3.1 目标

- 用户先确认候选表，再只发送已确认表的真实结构给 AI。
- AI 生成结果先预览和校验，用户明确点击后才写入编辑器。
- AI 永不自动执行 SQL。
- 简单查询降低延迟，复杂查询自动启用推理并分配更合理的 token 预算。
- 可自动修复的问题最多重试一次，避免无限重试和不可控成本。
- 失败时显示稳定诊断码、中文原因和下一步操作。
- 保留当前草稿，支持一轮自然语言修改，不建设完整聊天系统。
- 长 SQL 只影响编辑器内部，不再撑宽整个页面。
- 保持现有接口调用方兼容，新增字段均为可选或有默认值。

### 3.2 非目标

- 不建设异步 AI 任务队列、流式聊天、多轮会话历史或提示词版本管理系统。
- 不让 AI 自动选择并执行 SQL。
- 不发送样本行、字段值、连接信息、凭据或现有 SQL 之外的业务数据。
- 不修改数据源凭据模型、SecretStore、元数据缓存策略或数据库 schema。
- 不把 Provider 原始响应或推理过程保存到日志、审计或页面。
- 不借机重构整个 `SqlWorkspaceView.vue`、替换 Monaco 或升级依赖。

## 4. 用户体验设计

### 4.1 入口与布局

- 桌面端点击“AI 生成”后，在 SQL 编辑器右侧展开约 410px 的可折叠助手面板。
- 窄屏使用覆盖式 drawer，不能把编辑器压缩到不可用宽度。
- 面板关闭后保留本次草稿；切换数据源时清空提示词、候选表、生成结果和校验状态，防止跨数据源误用。
- 编辑器、结果区和工具栏仍是主工作区，助手不遮挡执行按钮。

### 4.2 分阶段交互

1. **描述需求**：用户输入自然语言。
2. **确认表范围**：系统本地推荐候选表，以可删除、可补充的标签展示；用户确认后才能生成。
3. **生成中**：展示当前阶段，例如“读取表结构”“生成 SQL”“校验 SQL”“自动修复”。
4. **预览**：展示只读 SQL、使用的表、只读/表名/列名校验徽标、推理模式和必要说明。
5. **应用**：用户点击“应用到编辑器”后替换当前编辑器内容；另提供复制按钮。
6. **单轮修改**：基于当前草稿输入“增加日期条件”等修改要求，重新预览；只保留最新草稿和最新修改指令。

面板不自动执行 SQL，也不把生成成功等同于数据库执行成功。

### 4.3 候选表推荐

候选表推荐是确定性、无 AI、无出站的辅助能力：

- 输入为当前数据源、可选 schema、自然语言关键词和当前编辑器中可识别的表名。
- 后端只读取表名和列名做轻量评分，返回有限数量候选项及命中理由。
- 前端只提交用户确认后的 `schema_name` 和 `table_names` 给生成接口。
- 生成接口必须重新从可信数据源元数据读取这些表，不能信任前端提交的列定义。
- 没有确认任何表时禁止生成，并提示用户选择表；不回退到“默认发送前 12 张表”。

推荐排序优先级：当前编辑器已引用表 > 表名关键词命中 > 列名关键词命中。相同分数保持元数据原始稳定顺序，不引入模糊模型或新依赖。

## 5. 后端数据流

```text
自然语言
  → 确定性候选表推荐（无出站）
  → 用户确认表范围
  → 后端重新读取真实 schema
  → 复杂度判定与推理模式选择
  → Provider 调用
  → Provider 响应归一化
  → SQL 提取与解析
  → 单语句/只读/表列校验
  → 必要时一次定向修复
  → 预览响应
  → 用户应用到编辑器
```

### 5.1 同步分阶段链路

本期保留同步 HTTP 调用。原因：只允许一次初始调用和最多一次修复，现有 Gateway 也是同步接口；引入异步 job 会显著扩大状态管理、取消、持久化和恢复范围。

前端显示阶段是请求内的逻辑阶段，而非实时服务端事件流。实际响应返回前显示“生成并校验”；响应中通过 `stage`、`attempts` 和 `validation` 解释最终经过的阶段。

### 5.2 真实元数据边界

- 候选表接口和生成接口均执行现有项目/数据源权限检查。
- 生成接口只读取用户确认表的 schema；读取失败继续 fail-closed。
- schema 上下文保持 L2 出站等级，不包含样本数据。
- 用户修改指令与当前候选 SQL 属于 L3；修改操作必须经过现有 AI egress policy，不能绕过 Gateway。
- 任何 metadata 或 egress 确定性失败均不调用或重试 Provider。

## 6. 推理模式与 Provider 兼容

### 6.1 自动推理选择

简单单表投影/过滤/排序/限制行数默认关闭推理，以降低延迟。出现以下任一信号时启用推理：

- 已确认多张表；
- 用户要求聚合、分组、窗口函数、子查询、CTE、集合运算或复杂日期口径；
- 修改指令要求改变查询结构；
- 规则无法确定是否简单。

不确定时保守启用推理。该分类器只基于确定性关键词和已确认表数量，不调用额外模型。

`AiOptions` 追加通用可选字段表达“推理偏好”，只有明确支持该能力的 Provider adapter 才转换为厂商参数。普通 OpenAI-compatible endpoint 不应收到未经声明支持的私有字段。

### 6.2 token 预算

- 简单模式使用较小预算，目标是快速返回只含 SQL 的最终回答。
- 推理模式使用明显高于现有 800 的预算，并为最终 SQL 留出余量。
- 首次响应为 `finish_reason=length`、只有推理内容或最终 content 为空时，一次重试提高预算并强化“只返回最终 SQL”的约束。
- 具体预算值在实施计划中通过 Provider 单测固定，不能由前端自由传入。

### 6.3 Provider 响应归一化

OpenAI-compatible adapter 需要解析并返回以下安全元数据：

- `content`；
- `finish_reason`；
- `reasoning_content` 是否存在及字符数，不返回其正文；
- prompt/completion/total token 数（Provider 有提供时）；
- Provider、模型、HTTP 状态分类和调用耗时。

Provider 原始响应、`reasoning_content` 正文和 HTTP 错误正文均不得进入日志、审计、API 错误或前端。

## 7. SQL 提取与安全校验

生成结果进入预览前必须依次通过：

1. 从 Markdown code fence 或纯文本中提取单条 SQL。
2. 使用项目既有 `sqlglot` 解析当前数据源方言。
3. 拒绝多语句。
4. 复用或对齐现有 SqlGuard 的只读规则，拒绝 DDL、DML、事务控制和不允许的命令。
5. 解析 CTE、表别名、子查询作用域，校验实际引用表在确认范围内。
6. 使用真实元数据校验可静态确定的列引用；`*`、派生列和无法静态判定的方言表达式要明确标记，而不是误报成功。

校验是生成预览的安全门，不连接数据库执行 SQL。通过校验也不代表查询一定能在数据库成功运行。

校验结果包含稳定代码、状态和安全展示信息：

- `readonly`: `passed | failed`；
- `tables`: `passed | failed`；
- `columns`: `passed | failed | partial`；
- `warnings`: 稳定代码列表，不包含 schema 原文或数据库底层异常。

## 8. 一次定向自动修复

每次生成或单轮修改最多两次 Provider 调用：初始调用一次，必要时修复一次。

- `finish_reason=length`、仅有推理内容或空 content：提高预算，要求只输出最终 SQL。
- SQL 格式或解析失败：将安全的校验错误代码和候选 SQL作为修复上下文，要求只返回修复后的 SQL。
- 未知表或未知列：只在模型输出与已提供 schema 不一致时修复一次。
- 鉴权失败、限流、网络不可达、超时、metadata 失败、egress 拦截不自动重试。
- 第二次仍失败则立即返回结构化诊断，不再循环。

修复调用仍经过 Gateway、egress 检查、脱敏和审计。

## 9. 诊断与审计

### 9.1 稳定诊断码

首期至少支持：

- `provider_auth_failed`
- `provider_rate_limited`
- `provider_timeout`
- `provider_unreachable`
- `provider_reasoning_only`
- `provider_output_truncated`
- `provider_invalid_response`
- `metadata_probe_failed`
- `ai_egress_blocked`
- `sql_parse_failed`
- `sql_not_readonly`
- `sql_unknown_table`
- `sql_unknown_column`

前端按诊断码显示本地化的“原因 + 建议动作 + 诊断编号”。后端 `error` 只保留兼容用安全短值；不得把异常字符串直接显示给用户。

### 9.2 审计字段

`ai_copilot_run` 审计只允许记录：

- 结果和诊断码；
- 最终阶段；
- 总耗时、Provider 调用耗时；
- 尝试次数；
- Provider 和模型；
- 推理模式；
- 已确认表数量；
- token 计数；
- egress level。

禁止记录自然语言、表名/列名列表、schema 内容、候选 SQL、最终 SQL、修改指令、Provider 原始响应、推理正文、连接参数或凭据。

## 10. API 兼容设计

### 10.1 候选表接口

新增：

`POST /api/datasources/{datasource_id}/ai/sql-table-candidates`

请求：

```json
{
  "natural_language": "string",
  "schema_name": "string | null",
  "editor_sql": "string | null"
}
```

`editor_sql` 只用于本地解析表名，不出站、不审计原文，长度沿用 SQL 工作台合理上限。

响应：

```json
{
  "candidates": [
    {
      "schema_name": "string | null",
      "table_name": "string",
      "matched_by": ["editor_reference | table_name | column_name"]
    }
  ],
  "truncated": false
}
```

### 10.2 生成接口

保留现有：

`POST /api/datasources/{datasource_id}/ai/sql-generate`

请求保持 `natural_language`、`schema_name`、`table_names` 兼容，并追加可选字段：

- `candidate_sql`: 当前草稿，仅用于单轮修改；
- `revision_instruction`: 本轮修改要求。

规则：`candidate_sql` 和 `revision_instruction` 必须同时出现或同时缺省；服务器限制长度，且将其视为 L3。

现有响应字段全部保留，追加：

- `stage`: 最终阶段；
- `diagnostic_code`: 稳定诊断码或 null；
- `attempts`: 1 或 2；
- `reasoning_mode`: `disabled | enabled`；
- `validation`: SQL 校验对象或 null；
- `request_id`: 用于支持定位的非敏感请求 ID。

新增字段必须有默认值，以免破坏现有 mock、测试和客户端。首次生成的
`egress_level` 保持 L2；带当前 SQL 和修改指令的单轮修改返回 L3。成功时 `ok=true`
且 `sql` 非空；失败时 `ok=false`、`sql=null`，并给出 `diagnostic_code`。

## 11. 代码边界

实施时保持以下职责分离：

- `app/api/schemas.py`：仅定义追加式 API 字段。
- `app/api/routes/core.py`：权限、元数据读取、调用编排和安全响应，不堆积 SQL 解析细节。
- `app/services/ai/providers.py`：Provider 请求与安全响应归一化。
- `app/domain/ai.py`：通用、向后兼容的 AI option/response 元数据。
- 新的纯服务模块：候选表确定性评分、复杂度分类、SQL 提取与校验、一次修复决策；不得 import 数据库驱动。
- `frontend/src/components/AiSqlAssistantPanel.vue`：面板交互和状态展示。
- `frontend/src/views/SqlWorkspaceView.vue`：只负责工作台集成、数据源切换重置和把 SQL 应用到 Monaco。
- `frontend/src/views/AppShellLayout.vue`：仅增加主内容宽度/溢出约束。

若实施中发现现有 SqlGuard 可直接复用，应优先复用；不得复制一套语义不同的只读规则。

## 12. SQL 工作台布局修复

布局改动限定为：

- AppShell 主内容 flex item 增加 `min-w-0`、`max-w-full` 和必要的横向 containment。
- SQL 工作区根节点、主编辑区和 Monaco 宿主明确 `min-w-0`、`max-w-full`、`overflow-hidden`。
- Monaco 长行使用编辑器自身横向滚动；是否换行继续遵循现有编辑器配置。
- AI 面板在桌面端占固定/受限宽度，剩余编辑区允许缩小；窄屏改为 overlay。
- 不对其他页面添加全局 `overflow-x:hidden` 来掩盖真实布局错误。

验收时 `document.documentElement.scrollWidth` 不应显著大于 `clientWidth`，允许浏览器取整产生 1px 误差。

## 13. 测试与验收

### 13.1 后端单元与契约测试

- Provider：正常 content、reasoning + content、仅 reasoning、`finish_reason=length`、空 choices、非 JSON、401/403、429、超时、网络不可达。
- 确认不会返回或记录 `reasoning_content` 正文和 HTTP 错误正文。
- 复杂度分类：简单单表关闭推理；多表、聚合、子查询、CTE、窗口函数开启推理。
- 自动修复：可修复错误只重试一次；确定性错误零重试；第二次失败返回最终诊断。
- SQL 校验：单条 SELECT、CTE、别名、子查询、未知表、未知列、多语句、DML/DDL。
- API：候选表权限/排序/截断，生成正常路径、修改路径、追加响应字段、旧请求兼容。
- 审计断言只包含允许字段，不包含 prompt、schema、SQL、Provider 正文或凭据。

### 13.2 前端 Playwright

- 打开/折叠右侧面板。
- 推荐候选表、增删并确认范围。
- 生成后显示校验状态，未点击应用前编辑器不变。
- 点击应用后编辑器更新，但不会自动触发执行接口。
- 单轮修改替换预览并只保留当前草稿。
- 切换数据源清空助手状态。
- 每个诊断码显示本地化原因和建议，不显示后端原始异常。
- 长 SQL 下页面整体无横向溢出，数据源选择、执行按钮和助手入口保持可见。
- Console 无红色错误，无 Vue/Vite 错误覆盖层。

### 13.3 质量门禁

- 后端：Ruff、format check、mypy、相关 pytest、全量 pytest、`make check-redlines` 的 Windows 等价命令。
- 前端：typecheck、build、相关 Playwright。
- 安全：`git diff`、`git status`、敏感词扫描、可用时运行 gitleaks。
- PR 创建后等待全部 GitHub Actions 通过，Agent 不合并。

### 13.4 真实环境验证边界

- 先用 mock Provider 覆盖全部确定性分支。
- D 盘部署只能在 PR 合并并同步后进行。
- 真实 Provider 验证会发送用户确认表的 schema 和自然语言，属于明确出站行为；执行前必须再次取得用户授权。
- 若现场数据源仍不可连接，则真实 schema/数据库执行链路记录为未验证，不用 mock 冒充真流程。

## 14. 发布与回滚

- 通过 feature branch 和 PR 交付，人工 review、人工 squash merge。
- 合并前不覆盖 D 盘实际使用环境。
- API 变更为追加式，不需要数据库迁移。
- 前端面板出现问题时可以回退前端提交；后端追加字段不会破坏旧客户端。
- Provider 归一化和诊断出现兼容问题时，可保持旧 Provider 调用参数，只禁用自动推理参数映射，不影响 AI Gateway 其他用途。

## 15. 已知基线风险

设计分支建立时验证结果：

- 后端全量测试通过：1180 passed，62 skipped，3 xfailed；跳过项主要需要真实数据库或真实 Provider 环境。
- 前端 typecheck/build 通过。
- 前端构建已有两处重复 i18n key 警告。
- `npm ci` 报告 3 个既有 high severity 依赖审计项。

这些基线问题不是本功能引入。本任务不顺带升级依赖；若后续改动触及相同 i18n 区域，应避免新增重复 key，并在最终验证中如实记录警告是否仍存在。
