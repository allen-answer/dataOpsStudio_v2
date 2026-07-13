# W Workflow 管理前端(2.0 workflow 域可视化落地) 设计稿

> 2026-07-07,Opus 4.8。方法:本仓 main 测绘 —— 2.0 workflow **后端已建成、前端零覆盖**的接入面
> (CRUD + run 触发/状态/取消 + C-9 通知配置 + C-7 变量流),逐条带 `文件:行号`;并测绘 2.0 前端既有
> 视图范式(Compare / Lineage / NavRail / api 层 / TanStack Query / i18n)供镜像。附一路只读 1.x 考古
> 结论。设计稿不出现 IP/账号/密钥/绝对路径。**本稿是给人 review 的提案,拿不准处显式标注,不臆造。**
>
> **结论先行**:2.0 workflow 后端**已相当完整**(CRUD `core.py:3349-3521`、run 触发/状态/取消
> `core.py:3847-3992`、C-9 通知配置 `core.py:3531-3653`、C-7 变量 `core.py:3867-3906`),但
> **前端完全没有 workflow 路由/视图**(router 只有 projects/datasources/sql/compare/lineage/jobs/admin,
> `router/index.ts:52-77`;NavRail 无 workflow 项 `NavRail.vue:26-32`)。这三块能力今天**只能靠 curl**。
> 提议新增 `WorkflowView.vue`,**完全照搬 2.0 Compare/Lineage 的建构范式**(单视图 master-detail、
> `api/workflow.ts` 锚 `schemas.py` 不臆造、TanStack Query + 轮询到终态、i18n 键、NavRail 加一项)。
> **v1 只做表单式节点编辑器 + 列表/编辑/触发/单 run 状态/通知配置面板;拖拽式 DAG builder 归 v2**
> ——理由:节点集只有 5 种(`workflow.py:30-38`)、payload 形状已被 compare.ts 等锚死,表单足够;
> DAG 可视编辑投入大、ROI 低,先让 workflow 能被人用起来。**首版三大缺口需后端补齐或前端绕行**
> (§1.6):**(a) 无"列 workflow 的 run 历史"端点**(只有按 run_id 查单个);**(b) 无节点 kind 元数据
> 端点**(builder 只能前端硬编码支持集 + payload 形状);**(c) PUT workflow 整份覆盖 dag_jsonb,
> 若 spec 漏带 `notifications`/`variables` 会静默清空**(§1.6.4,最需先拍板的坑)。

---

## 一、接入面考古(前端要消费的后端表面)

### 1.1 Workflow CRUD 端点 + 请求/响应 schema

`app/api/routes/core.py`(路由)+ `app/api/schemas.py`(契约):

| 方法 | 路径 | 请求 | 响应 | 位置 |
|---|---|---|---|---|
| GET | `/projects/{pid}/workflows` | `?limit&offset` | `list[WorkflowListItem]` | `core.py:3349` |
| POST | `/projects/{pid}/workflows` | `WorkflowCreateRequest` | `WorkflowResponse`(201) | `core.py:3374` |
| GET | `/projects/{pid}/workflows/{id}` | — | `WorkflowResponse` | `core.py:3427` |
| PUT | `/projects/{pid}/workflows/{id}` | `WorkflowUpdateRequest` | `WorkflowResponse` | `core.py:3444` |
| DELETE | `/projects/{pid}/workflows/{id}` | — | 204 | `core.py:3498` |

- `WorkflowCreateRequest`(`schemas.py:975`):`name: str(1..128)` + `spec: dict[str,Any]`(**故意用裸
  dict 不类型化** —— 路由层显式 `WorkflowSpec.model_validate` 好把 R7 门禁错误映射成结构化 4xx,
  见 `schemas.py:977-980`、`core.py:3736` `_validated_workflow_spec`)。`WorkflowUpdateRequest`
  (`schemas.py:983`)同形。★ **前端提交的 `spec` 必须整份是合法 `WorkflowSpec`**(见 §1.2)。
- `WorkflowResponse`(`schemas.py:988`):`id/project_id/name/spec: WorkflowSpec/enabled/schedule_cron/
  schedule_enabled/created_by/created_at/updated_at`。★ `spec` 是**完整回吐**的 WorkflowSpec
  (含 `nodes/edges/schedule/notifications/variables`),编辑页据此回填。
- `WorkflowListItem`(`schemas.py:1001`):列表轻量视图,含 `node_count`(后端 `_workflow_list_item`
  从 dag_jsonb 数,`core.py:3834`)、`schedule_cron/schedule_enabled/enabled`,**不含完整 spec**。
- 名称唯一性:同项目内重名 → 409 `workflow_name_conflict`(`core.py:3790`),前端要处理。
- R7 门禁错误码(创建 + enqueue 双门禁):`forbidden_node_kind` / `unsupported_node_kind` /
  `duplicate_node_id` / `unknown_edge_node` / `self_loop` / `cycle_detected` / `invalid_cron` 等
  (`workflow.py:107-116, 164-269`),路由映射成 400 + 具体 code(`core.py:3763` `_workflow_spec_error`)。
  ★ 前端应把这些 code 翻成人话内联提示。

### 1.2 `WorkflowSpec` 结构(编辑器要构造的对象)

`app/domain/workflow.py`:

- `WorkflowSpec`(`workflow.py:225`,**frozen**):
  - `nodes: list[WorkflowNode]`(`min_length=1, max_length=50`,`workflow.py:234`)——**至少一个节点**。
  - `edges: list[WorkflowEdge]`(默认空)——DAG 有向边,`{source, target}` 均须是已声明节点 id、
    无自环、无环(`workflow.py:249-269` 三色 DFS 找环)。
  - `schedule: CronSchedule | None`——`{cron: 5段, enabled}`(`workflow.py:137`,基本字符集校验,
    不引 croniter)。
  - `notifications: list[NotifyTarget]`(C-9,`workflow.py:238`)——**见 §1.4**。
  - `variables: dict[str, str|list[str]]`(C-7,`workflow.py:242`)——**见 §1.3**。
- `WorkflowNode`(`workflow.py:160`,frozen):`id`(非空)、`job_kind`(R7 双白名单)、
  `payload: dict[str,Any]`(**节点参数装这**,形状按 kind 而异)、`retry_policy: {max_retries 0-5,
  backoff_seconds 0-3600} | None`、`timeout_seconds(>=1)`、`on_failure: "abort"|"continue"`
  (`"branch"` 首版拒,`workflow.py:119-127`)、`when: str | None`(<=512 字符,存储 + 非空校验)。
- 支持的 `job_kind` 首版集 `SUPPORTED_WORKFLOW_NODE_KINDS_V1`(`workflow.py:30`):
  **`sql_query` / `sql_explain` / `compare_run` / `lineage_analyze` / `export_excel`**。R7 白名单
  `ALLOWED_WORKFLOW_NODE_KINDS`(`job.py:64`)更宽(含 notify/sleep/branch/scenario_*),但白名单内、
  支持集外 → `unsupported_node_kind`。★ **前端 builder 的可选 kind 应只列首版 5 种**。

### 1.3 变量(C-7)如何流入

- **spec 级默认变量**:`WorkflowSpec.variables`(`workflow.py:242`),`validate_workflow_variables`
  (`workflow.py:90`)校验:数量 <=32、key `^[A-Za-z_][A-Za-z0-9_]*$` 且禁撞内置名
  (`today/now/year/month/day`,`workflow.py:57`)、值是 `str` 或 `list[str]`,**值走保守安全字符集**
  `^[A-Za-z0-9_.:-]*$` 且禁 `--`(`workflow.py:76-87`,注入防御核心)。
- **触发时运行时变量**:`WorkflowRunTriggerRequest.variables`(`schemas.py:1053`,`dict[str, str|list[str]]`)
  经 `_validated_trigger_variables`(`core.py:3749`)同一套校验。
- **合并优先级**:`builtin < spec.variables < 触发时`(`core.py:3877-3881`),冻结进 run job payload 的
  `when_variables`(`core.py:3906`),run 生命周期内不漂移。
- ★ **前端两处要用**:(1) 编辑页维护 `spec.variables`(k-v);(2) 触发弹窗输入运行时 `variables`。
  **两处输入框都要内联提示安全字符集**(值只允许 `[A-Za-z0-9_.:-]`、禁 `--`),否则 400
  `unsafe_variable_value: <name>`(错误只含变量名不含值,R5,`workflow.py:82`),前端只能回显名。

### 1.4 通知目标配置(C-9 PR4)

子资源 CRUD(持久化进 `spec.notifications` → dag_jsonb,无独立表):

| 方法 | 路径 | 请求 | 响应 | 位置 |
|---|---|---|---|---|
| POST | `.../workflows/{id}/notifications` | `NotifyTargetCreateRequest` | `NotifyTargetResponse`(201) | `core.py:3531` |
| PUT | `.../workflows/{id}/notifications/{tid}` | `NotifyTargetUpdateRequest` | `NotifyTargetResponse` | `core.py:3569` |
| DELETE | `.../workflows/{id}/notifications/{tid}` | — | 204 | `core.py:3616` |

- `NotifyTargetCreateRequest`(`schemas.py:1021`):`channel: "webhook"|"wecom"`、**`url: str`(整条明文
  webhook/企微地址,企微 token 藏在 `?key=`)**、`events: list|None`、`enabled`、`timeout_seconds(1-60)`。
- ★★ **R2/R5 服务端边界**:路由收明文 `url` → **立即 `store_secret(url, WEBHOOK_SECRET)`**
  (`core.py:3551`)→ 配置里只落 `url_secret_ref`(`domain/notify.py:49`)。**`NotifyTargetResponse`
  (`schemas.py:1040`)没有 url 字段**(`core.py:3725` `_notify_target_response` 只吐
  `id/channel/events/enabled/timeout`)。→ **前端读取时永远拿不到、也绝不该显示明文 url**;
  只能显示渠道 + 状态,url 是"只写不回读"字段(改要重填整条)。
- 更新传 `url` 则重新 store 换 ref、删旧 secret(`core.py:3590-3601`);删除目标清理其 secret
  (`core.py:3639`)。前端 update 支持"只改 events/enabled 不动 url"(`url: None` 即不重存)。

### 1.5 Run 触发 / 状态 / 取消

| 方法 | 路径 | 请求 | 响应 | 位置 |
|---|---|---|---|---|
| POST | `.../workflows/{id}/runs` | `WorkflowRunTriggerRequest\|None` | `WorkflowRunCreateResponse`(201) | `core.py:3847` |
| GET | `/projects/{pid}/workflow-runs/{run_id}` | — | `WorkflowRunStatusResponse` | `core.py:3924` |
| POST | `/projects/{pid}/workflow-runs/{run_id}:cancel` | — | `CancelResponse` | `core.py:3946` |

- 触发:`WorkflowRun` 本身是 job(ADR-0009),`run_id == job_id`(`schemas.py:1068`);spec 快照冻进
  payload,触发后改定义不影响在途 run(`core.py:3860`)。禁用的 workflow → 409 `workflow_disabled`
  (`core.py:3872`)。可选 body `{variables}`(§1.3)。
- 状态:`WorkflowRunStatusResponse`(`schemas.py:1084`)= `run_id/workflow_id/project_id/status:
  JobStatus/error/created_at/started_at/finished_at/nodes: list[WorkflowRunNodeItem]`。
  `WorkflowRunNodeItem`(`schemas.py:1074`)= `node_id/job_kind/status(waiting/running/retry_wait/
  success/failed/skipped/cancelled)/job_id/attempts/error`。后端用与 worker 推进器**同一纯函数**
  `plan_workflow_step` 算节点态(`core.py:4090`),口径一致。
- 取消:软取消 run job + 传播到未终态子 job(`core.py:3974-3981`);已终态 → 409
  `workflow_run_terminal`(`core.py:3969`)。
- ★ **轮询范式**:run 是异步 job,前端照 Compare/SQL 的"轮到终态"范式(§2.3):触发拿 `run_id` →
  轮 `GET workflow-runs/{run_id}` 到 `status` 终态。节点级进度直接在 status 响应的 `nodes[]` 里,
  无需另拉。

### 1.6 UI 视角的缺口 / 别扭点(★ 提案需先拍板)

1. **无"列某 workflow 的 run 历史"端点**。只有 `GET workflow-runs/{run_id}` 查单个
   (`core.py:3924`)。通用 `/jobs`(`api/jobs.ts:36`)能按 `project_id/status` 过滤但**不能按
   `kind=workflow_run` 或 `workflow_id` 过滤**。→ **run 历史列表是硬缺口**。选项:(a) 后端新增
   `GET .../workflows/{id}/runs`(对齐 compare 的 `/compare/tasks/{id}/runs`,`core.py:1812`,
   最干净);(b) 前端只显示"本次触发的 run"(触发后跳详情),历史留 v2。**建议 (a),但需后端一个小 PR**
   ——本稿把它列为 workflow 前端的**前置后端依赖**(§四 PR0)。
2. **无节点 kind 元数据端点**。没有任何端点吐"支持哪些 kind、每种 kind 的 payload 形状/字段"。
   → builder **只能前端硬编码**首版 5 种 kind + 各自 payload 形状(compare_run 的 payload 形状
   即 `CompareDataRef`+rules+limits,已被 `api/compare.ts:52` 锚死可复用;sql_query 的 payload 形状
   需照 `_execute_*` 读取处反推)。**这是表单式编辑器 v1、放弃通用 builder 的核心理由**——形状不是
   机器可发现的,通用拖拽 builder 无从生成节点表单。**拿不准**:是否值得后端加一个
   `GET /workflow-node-kinds` 元数据端点(v2 再议)。
3. **spec 全量、无部分更新**。`WorkflowSpec.nodes` 至少 1 个(`workflow.py:234`);PUT 是整份
   `spec` 覆盖(`core.py:3466-3476`)。→ 编辑页是"取完整 spec → 本地改 → 整份 PUT 回",无 PATCH 单节点。
4. **★★ PUT 覆盖会静默清空 `notifications`/`variables`**。PUT workflow 用 `body.spec` 整份重写
   `dag_jsonb`(`core.py:3471`)。而 `notifications` 由**独立子端点**维护(§1.4)、`variables` 也在 spec 里。
   **若编辑 DAG 时前端 PUT 的 spec 没带上现有 `notifications`/`variables`,它们会被静默抹掉**(通知配置
   连同其 SecretStore ref 一起丢,secret 变孤儿)。→ **编辑页保存前必须先 GET 完整 spec、原样保留
   `notifications` 与 `variables`,只改 nodes/edges/schedule/variables 目标字段再整份 PUT**。
   **拿不准**:这是后端设计的耦合坑,是否该后端在 PUT 时"若 body 未显式给 notifications 则保留原值"?
   本稿倾向**前端先照直保留**(不改后端),但把它标为**最高优先要人确认的红旗**。
5. **通知目标"只写不回读 url"**(§1.4)。前端列表只能显示渠道 + 状态,url 是黑盒;"编辑 url"实为
   "重填整条"。UI 要明确措辞(如占位符"留空则不修改地址")。
6. **`notifications` 的读取来源不对称**。列现有目标要从 `GET workflow` 的 `spec.notifications` 读
   (其中每项含 `url_secret_ref` 这个内部指针,**前端不显示它**),而增改删走子端点 → 增改删后需
   invalidate workflow query 重取。

### 1.7 1.x 考古结论(前端范式无可移植)

只读 `ssh daily-server ~/dataops-studio`(相对路径引用):1.x **有** workflow 执行产物
(`results/workflow_runs/<id>/exports/*.xlsx` 大量存在,证明 1.x workflow 真跑过并导出),但
**`frontend/src/views` 下无任何 workflow/dag/pipeline 视图**(grep 无命中)。→ **1.x 没有可镜像的
Vue workflow 管理 UI**(1.x workflow 是 API/后端驱动)。故 2.0 前端**不参考 1.x,直接照 2.0 自己的
Compare/Lineage 范式建构**(§二)。

---

## 二、前端范式(镜像 2.0 既有视图)

### 2.1 视图 / 路由 / 导航

- 视图单文件:`frontend/src/views/CompareView.vue`、`LineageView.vue` —— **单视图 master-detail**
  (左列表/右详情),不是多路由子页。`WorkflowView.vue` 照此。
- 路由:`router/index.ts:52-71` 每业务页一条 `projects/:id/<名>`,`component` 懒加载。
  → 加 `{ path: 'projects/:id/workflows', name: 'workflows', component: WorkflowView }`。
- 导航:`components/NavRail.vue:26-32` 有项目时 push compare/lineage/jobs 项,每项 `{name, icon
  (lucide), to:{name, params:{id}}}`。→ 加一项(如 `Workflow`/`GitBranch` 图标)。

### 2.2 API 层

- 每域一个 `api/<域>.ts`,**顶部注释块显式声明"字段锚自 `schemas.py` 哪些类、端点表",不臆造**
  (`api/compare.ts:1-33` 是范本)。类型 = 后端 schema 的手抄镜像;函数 = 薄封装 `apiClient.get/post/...`。
- → 新建 `api/workflow.ts`:镜像 `WorkflowListItem/WorkflowResponse/WorkflowCreateRequest/
  WorkflowUpdateRequest/WorkflowRunTriggerRequest/WorkflowRunCreateResponse/WorkflowRunStatusResponse/
  WorkflowRunNodeItem/NotifyTargetCreateRequest/NotifyTargetUpdateRequest/NotifyTargetResponse` +
  `WorkflowSpec/WorkflowNode/WorkflowEdge/CronSchedule` 结构。**compare_run 节点 payload 直接复用
  `api/compare.ts` 的 `CompareDataRef` 等类型**(不重抄)。
- `apiClient`(`api/client.ts`)已封装 auth/错误;沿用。

### 2.3 TanStack Query + 轮询

- 列表/详情用 `useQuery`(`CompareView`/`JobsView.vue:35`);变更后 `queryClient.invalidateQueries`。
- **异步 job 轮询**:Compare/SQL 的范式是"触发拿 run_id/job_id → 轮询到终态"。TanStack 支持
  `refetchInterval`(run 未终态时返回间隔、终态返回 false 停轮)。→ workflow run 详情 query 对
  `GET workflow-runs/{run_id}` 设 `refetchInterval`,`status ∈ {success,failed,cancelled,timeout}`
  即停。节点级进度在同一响应 `nodes[]`,无需额外请求。
- `getJob`(`api/jobs.ts:45`)是既有单 job 轮询参考。

### 2.4 i18n

- `i18n/zh-CN.ts` / `en.ts` 分域嵌套(`compare:{...}` 在 `zh-CN.ts:334`)。→ 加 `workflow:{...}` 域,
  含 R7/校验/通知错误码 → 人话映射(§1.1 的 code 列表)。NavRail label 也走 i18n key。

---

## 三、提议 UI 范围

### 3.1 Workflow 列表(左栏)

- `GET .../workflows` → 卡片/行:name、node_count、schedule(cron + enabled 徽标)、enabled、updated_at。
- 操作:新建、点选进详情、删除(确认弹窗,204)。重名 409 内联提示。

### 3.2 创建 / 编辑(右栏,表单式节点编辑器 —— v1 核心决策)

- **表单式,非拖拽**(§1.6.2 理由:5 种 kind、payload 形状非机器可发现)。
- **节点编辑**:一个节点列表,每节点表单 = `id` + `job_kind`(下拉,只列首版 5 种)+ 按 kind 渲染的
  payload 子表单(compare_run 复用 compare 的 ref/rules 控件;sql_query = 数据源 + SQL;等)+
  `timeout_seconds` + `on_failure`(abort/continue)+ 可选 `retry_policy` + 可选 `when`。
- **边编辑**:v1 用"每节点选它的上游节点"下拉(等价声明 edges),前端组装 `edges[]`;**不做画布连线**。
  提交前本地跑一遍轻校验(id 唯一、无自环)给即时反馈,真校验以后端 400 为准。
- **schedule**:可选 cron 输入(5 段)+ enabled 开关。
- **spec.variables**:k-v 编辑区(§1.3,值内联安全字符集提示)。
- ★ **保存**:GET 完整 spec → 本地改 nodes/edges/schedule/variables → **原样保留
  `notifications`**(§1.6.4 红旗)→ 整份 PUT。
- **v2 deferred**:拖拽 DAG 画布、节点 kind 元数据驱动的动态表单、`branch`/`notify`/`sleep` 节点、
  部分更新。

### 3.3 Run 触发 + C-7 变量输入

- "运行"按钮 → 弹窗:可选运行时 `variables`(k-v,值走 §1.3 安全字符集,内联校验;禁 `--`)→
  POST `.../runs` 拿 `run_id` → 跳该 run 详情并开始轮询。
- 禁用 workflow 的运行按钮置灰(避免 409 `workflow_disabled`)。

### 3.4 Run 历史 / 状态

- **单 run 状态**(v1 必做):轮 `GET workflow-runs/{run_id}` 到终态;渲染 run status + 节点表
  (node_id / kind / status 徽标 / attempts / error);未终态时"取消"按钮 → `:cancel`。
- **run 历史列表**(依赖 §四 PR0 后端端点):某 workflow 的历次 run 倒序(status/触发时间/耗时)。
  **PR0 未落地前 v1 只显示"刚触发的 run"**(触发即跳详情),历史留 v2。

### 3.5 通知目标配置面板(C-9)

- workflow 详情内一个"通知"分区:从 `GET workflow` 的 `spec.notifications` 列现有目标
  (显示 channel + events + enabled;**绝不显示 url / url_secret_ref**)。
- 增:选 channel(webhook/wecom)+ 填**明文 url**(明确标注"仅提交、不回显")+ events + timeout →
  POST notifications。改:events/enabled 就地改(不传 url);"更换地址"= 重填整条 url。删:DELETE。
- 每次增改删后 invalidate workflow query 重取 `spec.notifications`。

### v1 vs 后延

| 能力 | v1 | 后延(v2) |
|---|---|---|
| 列表 / 创建 / 编辑 / 删除 | 表单式 | 拖拽 DAG 画布 |
| 节点 kind | 首版 5 种硬编码 | 元数据端点驱动 / branch·notify·sleep |
| 边 | 上游下拉声明 | 画布连线 |
| 触发 + 运行时变量 | 有 | 变量模板/预设 |
| 单 run 状态 + 取消 | 有 | — |
| run 历史列表 | 依赖 PR0 后端端点 | 若 PR0 不做则 v2 |
| 通知配置面板 | webhook/wecom | 邮件(后端 C-9 PR5 后) |
| schedule cron | 输入 + enabled | cron 可视化/下次触发预览 |

---

## 四、分 PR 建议(增量、可 review)

| PR | 内容 | 依赖 |
|---|---|---|
| **PR0(后端,前置)** | 新增 `GET .../workflows/{id}/runs`(对齐 `/compare/tasks/{id}/runs`,`core.py:1812`)供 run 历史。**若砍掉则 run 历史归 v2**,前端 PR3 只做单 run | 独立小 PR |
| **PR1** | `api/workflow.ts`(锚 schemas.py,复用 compare.ts 类型)+ 路由 + NavRail 项 + i18n 骨架 + 只读**列表 + 详情(spec 展示)**。真后端点通列表/详情 | 与 PR0 无关 |
| **PR2** | 创建/编辑:表单式节点编辑器 + 边(上游下拉)+ schedule + `spec.variables`。★ 保存保留 `notifications`(§1.6.4)。R7/校验错误码内联 | PR1 |
| **PR3** | 触发(+ 运行时 variables 输入)+ 单 run 状态轮询 + 取消 +(PR0 就绪则)run 历史列表 | PR1(+PR0) |
| **PR4** | 通知配置面板(增改删,明文 url 只提交不回显)| PR1 |

每个 PR 走 CLAUDE.md §4 前端验证(§六)。

---

## 五、红线注意

- **R5(禁敏感值回显/入日志)——本前端最相关**:
  - **通知 url / 企微 token 绝不回显**:后端 `NotifyTargetResponse` 已无 url 字段(`core.py:3725`),
    前端**列表/表单都不得显示明文 url、也不得显示 `url_secret_ref`**;"编辑地址"只能重填。
    console.log / 错误提示禁打 url。
  - **变量值错误只回显名不回显值**:后端 `unsafe_variable_value: <name>` 已只含名(`workflow.py:82`);
    前端提示照抄,别把用户输的值再拼进 toast。
- **R2(密钥只持 SecretRef)——边界在服务端,前端只递明文一次**:前端把明文 webhook/SMTP url 提交给
  配置 API 是**设计允许的唯一入口**(`schemas.py:1022`),服务端 `store_secret` 换 ref
  (`core.py:3551`)。前端**不落地、不缓存、不回读**该明文(提交后清空表单字段)。前端不碰
  SecretStore、不碰 ref 语义。
- **R7(节点 kind 白名单)——前端只做体验兜底,真门禁在后端**:builder 下拉只列
  `SUPPORTED_WORKFLOW_NODE_KINDS_V1`(避免用户提交必被拒的 kind),但**这是体验优化不是安全边界**
  ——真校验在后端 `WorkflowSpec.model_validate` 双门禁(创建 `core.py:3386` + enqueue `core.py:3874`)。
  前端不得假设"下拉限制过了就安全",仍要正确呈现后端 `forbidden_node_kind`/`unsupported_node_kind`。
- **注入(C-7 值)**:变量值安全字符集在**后端强制**(`workflow.py:76`);前端做**同规则内联预校验**
  仅为即时反馈,不替代后端。

---

## 六、测试要求(CLAUDE.md §4 前端 bar)

- **typecheck + build**:`vue-tsc` 通过、`vite build` 通过(★ 但 build 通过 ≠ 跑起来)。
- **真后端点通(非 mock)**:launcher 起 API+worker+PG(或 SSH 隧道连真服务器),浏览器实操:
  1. NavRail 进 workflow 页,F12 Console 无红错,列表真返回数据(或空态)。
  2. 创建一个含 compare_run 节点的 workflow → 201,列表/详情回填正确。
  3. 触发(带一个运行时变量)→ 201 拿 run_id → 轮询看节点从 running 到终态、run status 终态。
  4. 取消一个在途 run → run 转 cancelled;已终态 run 取消 → 409 提示正确。
  5. 加一个 webhook 通知目标(填明文 url)→ 201;**刷新后列表不显示 url**;改 events、删除各走通。
  6. 触发禁用/错误 spec → 后端 4xx(`workflow_disabled`/`forbidden_node_kind`/`cycle_detected` 等)
     内联提示正确。
  - **贴真证据**:响应体片段 / 网络面板 / DOM 截图 / curl+stdout(不写"应该没问题")。
- **R5 回归(手测 + 断言)**:创建带 token 的 webhook 目标后,**网络面板 + DOM + console 均无明文 url**。
- **编辑保留通知(§1.6.4)**:给 workflow 配了通知目标后,编辑 DAG 并保存 → 重取 `spec.notifications`
  **仍在**(证明前端保存时保留了它,没被 PUT 清空)。**这是必测项**。
- CI:后端集成测试走 PG(R9),不跑 SQLite。

---

## 七、范围建议(v1 优先级)

1. **先让 workflow 能被人用起来**:PR1(列表/详情)+ PR2(表单式创建/编辑)+ PR3(触发 + 单 run 状态)
   ——这三步就把"今天只能 curl"变成"能在 UI 建、跑、看",ROI 最高。
2. **通知面板(PR4)紧随**:C-9 后端已就位,面板小、价值高(run 失败告警是运维刚需)。
3. **run 历史(PR0 + PR3 的历史列表)** 视后端 PR0 是否做:**建议做 PR0**(一个小端点),否则 run 历史
   体验缺一块;砍了就 v1 只看单 run,历史归 v2。
4. **拖拽 DAG builder、节点 kind 元数据端点、邮件通知、branch/notify/sleep 节点** —— **全部 v2**。
   首版 5 种 kind + 上游下拉声明边,足够覆盖"定时对比 + 导出 + 血缘"这类真实编排。

---

## 八、拿不准(显式待人拍板)

1. **§1.6.4 最高优先**:PUT workflow 整份覆盖会静默清空 `notifications`/`variables`。前端照直保留
   (不改后端)够不够?还是应后端在 PUT 未显式给 notifications 时保留原值?**建议后端补一层保护**,
   但需人确认(涉及后端行为改动)。
2. **§1.6.1**:是否做后端 PR0(`GET .../workflows/{id}/runs`)?做 → run 历史 v1 就有;不做 → 归 v2。
3. **§1.6.2**:是否值得后端加 `GET /workflow-node-kinds` 元数据端点(为 v2 动态表单/拖拽 builder 铺路)?
   v1 不需要,v2 前置。
4. **compare_run 之外节点的 payload 形状**:sql_query / sql_explain / lineage_analyze / export_excel 的
   payload 字段没有前端可直接复用的 TS 类型(compare_run 有 `api/compare.ts`)。v1 是否只先支持
   **compare_run + sql_query** 两种节点表单,其余 kind 留 v2?——**倾向是**(聚焦最常用编排),待确认。
5. **run 详情节点点开**:是否让节点行链到其子 job 结果(compare 结果/SQL 结果)?v1 可只显示状态+error,
   深链归 v2。

---

## 附:关键接入点速查

| 事项 | 位置 |
|---|---|
| Workflow CRUD 端点 | `app/api/routes/core.py:3349-3521` |
| CRUD 请求/响应 schema | `app/api/schemas.py:975-1011` |
| `WorkflowSpec` / `WorkflowNode` | `app/domain/workflow.py:225, 160` |
| 首版节点集 / R7 白名单 | `app/domain/workflow.py:30` / `app/domain/job.py:64` |
| spec 校验错误码映射 | `app/api/routes/core.py:3736, 3763` |
| C-7 变量校验 + 安全字符集 | `app/domain/workflow.py:90, 76` |
| 变量合并优先级(触发) | `app/api/routes/core.py:3877-3906` |
| C-9 通知子端点 | `app/api/routes/core.py:3531-3653` |
| 通知 schema(响应无 url) | `app/api/schemas.py:1021-1047` / `core.py:3725` |
| `NotifyTarget`(url_secret_ref) | `app/domain/notify.py:38-49` |
| run 触发 / 状态 / 取消 | `app/api/routes/core.py:3847, 3924, 3946` |
| run/节点状态 schema | `app/api/schemas.py:1053-1093` |
| 节点态与 worker 同源纯函数 | `app/api/routes/core.py:4090` `plan_workflow_step` |
| 前端视图范式 | `frontend/src/views/CompareView.vue`、`LineageView.vue` |
| 路由 / 导航 | `frontend/src/router/index.ts:52-77`、`frontend/src/components/NavRail.vue:26-32` |
| api 层范式(锚 schema) | `frontend/src/api/compare.ts:1-33`、`api/jobs.ts:45` |
| i18n 分域 | `frontend/src/i18n/zh-CN.ts:334` |

---

## 九、2.4.x 完成补记（2026-07-13）

本轮在既有 Workflow 主从视图和统一 API 网关内完成结构化定义编辑器；不新增前端路由，也不改变后端领域边界。本节取代上文关于“五种节点、JSON 主编辑、Email/branch/notify/sleep 延后”的历史规划口径：

- 节点编辑器覆盖 `sql_query`、`sql_explain`、`compare_run`、`lineage_analyze`、`export_excel`、`notify`、`sleep`、`branch` 八种实际支持的 kind，并保留超时、重试、节点 `when` 与失败策略。
- 执行顺序以编号轨道表达；普通、条件、默认和 failure 边都可编辑，first-match 路由可上下排序。Branch success 至少两条边；failure branch 至少一条边；两者都必须且只能有一个 default。
- 结构化表单是主入口；同页“高级 DAG JSON”用于无损往返节点、边、schedule、sensor 与变量。通知目标继续由通知子资源管理，内部 secret ref 不进入 JSON 文本区或 Workflow 保存请求；后端在 Workflow 行锁内合并当前通知数组，避免长时间打开的草稿恢复已轮换的旧引用。
- Workflow 可编辑 `enabled`、五段 cron、SQL Sensor、标量/列表变量。Compare 节点快照排除当前执行路径不支持的文件型任务；Export 从上游 SQL/Explain 节点生成 `${nodes.<id>.result_set_id}` 并补齐 success 边。
- 变量单值/列表项与后端一致最多 512 字符。Export 通过实际 SQL/Explain 节点 id 精确匹配来源，节点 id 含 `.` 时仍可无损回填，同时继续要求直接 success 边。
- 新建 Workflow 时 Notify 不可选；首次保存后先在同一详情页配置目标，再添加 Notify。通知目标支持 Webhook、企业微信和 Email；URL/SMTP 密码均只写，提交或关闭后立即清空。
- 删除仍被 Notify 节点引用的目标、或更新/删除被 pending/running Run/Sensor 冻结的目标会返回结构化 409；页面翻译该冲突及全部 Workflow 域校验错误码，不回显原始表达式或 SecretRef。Workflow 保存、通知目标增改删、手动触发、cron 与 sensor 都以同一 Workflow 行锁串行化，并在锁后读取当前 spec，防止丢更新、孤儿 SecretRef 或冻结旧引用。
- Run 历史按 20 条做 offset 分页，节点详情只渲染 API 返回的安全标量 outputs。

继续后延：拖拽画布、动态节点元数据端点、Scenario、单 Workflow 时区、HA/backfill、任意 HTTP 节点和复杂结果浏览器。
