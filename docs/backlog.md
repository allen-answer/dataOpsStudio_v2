# DataOpsStudio 2.0 Backlog

> review 过程中 surfaced 的"暂不阻塞、但要记得"项。每一条标明**最迟修复版本**和**触发条件**;到该版本前必修的标 ★ 必修。

## 来自 T4 (API + middleware) review

### **T5 做 License 时补** — Middleware 只检 REPAIR,没处理 IN_GRACE

**位置**:`app/api/middleware/crosscutting.py:_check_license`

**现状**:`_check_license` 只在 `LicenseMode.REPAIR` 时拦截 `_REPAIR_RESTRICTED` 路径(POST /api/datasources / /sql/execute / /datasources/{id}/test)。

**遗漏**:设计稿 §8.4 中 `IN_GRACE` mode(过期 ≤7 天)= **只读 + 允许 license 更新 + 备份;禁建任务 / 禁 AI**。当前 middleware 未对 IN_GRACE 做任何检查。

**修法**:T5 实现 License Skeleton + Repair Mode 时,把 `_check_license` 扩为对 `IN_GRACE` 也限制写操作(同 `_REPAIR_RESTRICTED` 路径集),允许只读 GET。

**触发条件**:T5 工作启动时。**优先级**:中(license 任意非 REPAIR 状态都该被覆盖到位)。

---

### **可选** — 手写 JWT(HS256)→ PyJWT

**位置**:`app/api/security.py`

**现状**:Codex T4 手实现 HS256(hmac + hashlib + base64),`hmac.compare_digest` 用对了(时序安全),代码逻辑 OK。

**为什么记录**:手写 crypto 是 audit smell。PyJWT / python-jose 是 battle-tested,有维护团队,默认更严(reject `alg: none` 等)。

**修法**:`uv add pyjwt` → `security.py` 切 PyJWT API(create / decode / 处理 jwt.ExpiredSignatureError 等)。

**优先级**:低,可选。当前手写实现没漏洞(看过)。如做 GA 前安全审计时被点,即换。

---

### **F2 正解** — Worker 独立心跳线程(取消 OLAP 取舍)

**位置**:`app/worker.py:WorkerRunner`

**现状**:heartbeat 只在 fetch loop 内每 15s 触发,慢首行 OLAP 期间不触发。2.0.0 通过把 `worker_heartbeat_timeout` 调到 600s 缓解(见 ADR-0018):**避免误杀合法慢查询**优先于 **worker 死亡后更快恢复**。

**Trade-off 痛点**:600s = 10 分钟,实际 worker 崩溃后需 10 分钟才 reaper 接管。对生产场景偏长。

**正解**:独立 heartbeat 线程/定时器,与 fetch loop 解耦。线程每 15s 强制 heartbeat,即使主线程在 driver-level 阻塞首行返回。

**触发条件**:
- 客户 hosted 形态出现"worker 死了但 reaper 10 分钟才恢复"投诉
- 或 hosted 上需要把 timeout 压回 90s 提升故障恢复速度(2.6.0 hosted Copilot 上线后)

**优先级**:不阻塞 2.0.0 / 2.0.x,**2.7.0 hosted scale-out 前必修**(那时 worker fleet 规模化,单 worker 死 10 分钟意味着可见的吞吐损失)。

**关联**:ADR-0018 已记录此 trade-off + 引用本 backlog 项。

---

### **错误体验** — worker 失败时写结构化对外 error code

**位置**:`app/worker.py` / `app/infrastructure/jobbackend/postgres.py` / `app/api/routes/core.py`

**现状**:`jobs.error` 存 worker 捕获到的原始 `str(exc)`,可能是 DB driver 原文、SQL/schema 信息或连接细节。`GET /api/jobs` 列表端点因此不能直接返回 `jobs.error`,只能基于 `status/kind` 做粗分类(`sql_execution_failed` / `datasource_connection_failed` / `query_timeout` 等)。

**不够用**:同为 `sql_query` 的连接失败和 SQL 执行失败目前无法结构化区分;猜 DB 原文 substring 不可靠且有泄敏风险。

**修法**:worker fail 时写结构化对外 error code(连接 / SQL / 超时 / 权限 / 内部错误等),DB 原文只进审计或脱敏日志。API 列表和详情端点直接返回结构化 code,不在端点侧猜原文。

**交叉引用**:T7 Part A P0 已先加 `DatasourceTestResponse.error_code` 留位枚举,但 2.0.0 测连失败仍只稳定产出 `unknown` / `timeout`;`auth_failed` / `host_unreachable` / `permission_denied` 等细分类和多方言 adapter / `Column.type` 统一枚举一起做。

**触发条件**:T7 错误体验/任务列表 polish 或 GA 前错误分层审计时。

**优先级**:中。当前列表端点已安全但不够精确。

---

## 来自列元数据通道 review(66715f7)

### **GA 前 / 多方言适配器对齐时补** — `Column.type` 字段是 driver-specific 字符串

**位置**:`app/dbclients/mysql_adapter.py:_description_to_columns`

**现状**:`type=str(type_value)` 取自 `cursor.description[1]`,对 PyMySQL 是 FIELD_TYPE 整数常量,字符串化后前端拿到的是 `"253"` / `"<class 'pymysql.constants.FIELD_TYPE.LONG'>"` 之类。

**够用**:2.0.0 骨架"看得到列名"够用,不阻塞 §5 e2e。

**不够用**:前端无法按类型染色 / cast / format(日期、数字、bytes 都看不出区别)。Oracle / DM / DB2 adapter 上线后,每方言的 type code 编码各不相同,前端无法统一处理。

**修法**:定义内部 `ColumnType` 枚举(契约新增章节),各 adapter 把 driver-specific code 映射到统一枚举(string / integer / float / decimal / boolean / datetime / date / time / bytes / json / unknown)。`Column.type` 改为枚举字段,原始 driver 类型放 `Column.driver_type: str | None` 备查。

**触发条件**:
- 第二个 adapter(Oracle / DM / DB2 任一)合并时,**必须**同时引入统一枚举(否则前端代码会被迫做 N 方言条件分支)
- 或 GA 前前端 SQL Workspace 做"按类型染色"功能时

**优先级**:中高,产品必需(但不阻塞骨架)。**最迟修复版本:2.0.0 GA 前**。

**关联**:契约 §3.2 DatabaseAdapter Protocol,设计稿 §2.6 SQL Workspace 列展示。

---

## 来自首次云服务器 dogfood(5f27cc5,§5 真链路跑通)

### **T5 启动时配齐** — license.lic 缺失,T5 一启 middleware 会拦下所有写

**位置**:`config/license.lic`(BootstrapPaths.license_file)

**现状**:dogfood 跑通的前提是 license middleware 当前**对 IN_GRACE 没处理 + 没 license.lic 时按某种 default mode 通过**(具体 default 行为见 T5 实现)。一旦 T5 把 license 检查严格化,这套 dogfood 配置会因为没 license.lic 而 503 / 403。

**修法**:T5 工作启动时:
1. 生成 dev / dogfood 用的 license.lic(TRIAL 模式,长效期)
2. T6 `dataops bootstrap init` 顺手生成 dev license
3. 启动期没 license 时,launcher 提示而非神秘 503

**触发条件**:T5 工作启动时。**优先级**:T5 工作启动时必同步。

**关联**:本 backlog 上面"T5 做 License 时补 — Middleware 只检 REPAIR,没处理 IN_GRACE"是同一团事的另一面。

---

### **GA 前安全评审会被点** — `POST /api/datasources` 密码以明文进 request body

**位置**:`app/api/schemas.py:DatasourceCreateRequest.password`

**现状**:datasource 创建必须把目标 DB 的明文密码放进 JSON body。HTTPS 下 OTW 安全,但**浏览器历史 / 客户端 proxy log / FastAPI 错误堆栈 / nginx access log(默认不记 body 但配错可能记)** 都是潜在落点。

**够用**:服务器收到后立刻 Fernet 加密入库 + 响应不返密码(R2 红线已守住,dogfood 验证过)。

**不够用**:GA 前安全审计会点"密码进 request body"。

**修法选项**:
- 分两步建:先建 datasource(无密码,占位 secret_ref),再独立 `PATCH /datasources/{id}/password`(明确审计 + 短 TTL token)
- 或前端先 `POST /secrets`(获 secret_ref),建 datasource 时只传 secret_ref

**优先级**:**低**,GA 前安全评审窗口决定。dogfood / dev 不挡路。

---

### **T6 portable 形态文档化** — stop 语义 / systemd / 日志 rotate / LE TLS

dogfood 实测发现的部署文档项,逐条 T6 / 部署文档要覆盖:

- **graceful stop**:`tmux kill-session` 可能打断正在跑的 job。worker.py 已 `import signal`,但**没文档化"SIGTERM 是 graceful,会等当前 job complete 再退"**。T6 必须暴露 `dataops stop` 命令 + 文档说明信号语义。
- **systemd unit**:tmux 是 dogfood 临时态。正式部署需要 `dataops.service` / `dataops-worker.service` 单元文件 + journald 接管日志。
- **log rotation**:`tee` 到文件无 rotate,长跑会撑满。systemd journald 自动 rotate 是顺路解决方案。
- **TLS**:自签 cert 在 dogfood 够,正式部署要 Let's Encrypt 路径文档(1.x 的 nginx-rp 注释里说"备案后切",T6 部署文档延续这套就行)。

**触发条件**:T6 工作启动时。**优先级**:T6 launcher 覆盖 portable 形态时一并。

---

## 来自 T7 前端 review(2δ — 前端已追平现有 5 组 endpoint 能力上限)

> **背景**:T7 phase 2δ 做数据源 §3 时逐条核对 PRD vs 后端,确认 **Part A 前端已基本追平
> 现有 5 组 endpoint(`auth/login` / `projects` / `datasources` / `sql/execute` / `jobs`)
> 能支撑的全部范围**。下列 PRD 功能**全部卡在后端缺端点 / 缺字段**,不是前端漏做 ——
> 前端侧已用 `★ 后补` 注释在对应位置标好,后端补齐即可直接接上(多数无需前端二次改)。

### **T7 前端做满 §3 需要** — 数据源 编辑 / 删除 端点缺失

**位置**:`app/api/routes/core.py`(datasources 路由组)

**现状**:datasources 只有 `POST`(建)/ `GET`(列 + 详情)/ `POST .../test`。**无 `PUT` / `PATCH` / `DELETE`**。

**卡住的前端**:
- 编辑数据源 modal(PRD §3「编辑 modal」:密码留空 = 不改;名称/类型改动失效旧连接池)→ 需 `PUT /datasources/{id}`
- 删除数据源 + 引用检查(PRD §3「删除若被 task/workflow 引用则拒绝,列出引用者」)→ 需 `DELETE /datasources/{id}`,被引用返 409 + 引用者列表

**前端就位情况**:`DatasourcesView.vue` hover 操作行已留 Edit/Delete 注释占位(只待端点)。

**触发条件**:T7 要把 §3 数据源页做"满"时。**优先级**:中。

---

### **T7 §3 权限面板需要** — 无 `operation_policy`(8×allow_*)字段

**位置**:`app/api/schemas.py:DatasourceCreateRequest` + `app/db/models.py:datasources`

**现状**:create 只有 `extra: dict`(→ `capability_profile`,worker 读作 adapter **连接子节**,
如 ssl_mode / service_name)。**没有独立的 `operation_policy` / 8 个 `allow_*` 字段**;
现在前端建一组开关 POST 上去也**无处落、worker 不读**。

**卡住的前端**:PRD §3「权限」折叠面板 8 个开关(allow SELECT / EXPLAIN / Oracle PLAN_TABLE /
DM EXPLAIN / schema 导入 / schema 写入 / scenario 写入 / 自动建对比任务)= 设计稿 §4.5 operation_policy。

**修法**:schema + 表加 `operation_policy`(8 bool),worker 执行前据此校验操作类型。
**前端就位情况**:`DatasourcesView.vue` 新建表单已留 8×allow_* 折叠面板注释占位。

**触发条件**:设计稿 §4.5 operation_policy 落地时(可能 T5 权限模型一并做)。**优先级**:中。

---

### **T7 §4 SQL 跑非 MySQL 需要** — worker 仅支持 MySQL

**位置**:`app/worker.py:402`(`if conn_info.db_type is not DbType.MYSQL: raise UnsupportedDbTypeError`)

**现状**:2.0.0 worker **只实现 MySQL adapter**;PostgreSQL / Oracle / DM / DB2 数据源跑 SQL 直接
`UnsupportedDbTypeError`。

**卡住的前端**:SQL 工作台(§4)对非 MySQL 数据源执行 → 任务必 failed。前端**行为正确**
(正常提交 → 轮询 → 显示 failed),但用户体验是"建了 PG 数据源却跑不了"。

**修法(二选一)**:
- 后端:补 PostgreSQL(及其它方言)worker adapter
- 前端兜底(轻量先行):SQL 工作台选到非 MySQL 数据源时,执行按钮旁提示"当前仅支持 MySQL 执行",
  避免用户提交后才看到 failed(**此项前端可独立做,不阻塞后端**)

**触发条件**:多方言执行上线前 / 或先做前端 warn 兜底。**优先级**:中(2γ e2e 已实锤此限制)。

---

### **T7 §5 Jobs 错误码字典需要** — 见下方「worker 失败时写结构化对外 error code」

§5 任务历史的「结构化错误码字典」(PRD §5)直接依赖 backlog 已有项
**「错误体验 — worker 失败时写结构化对外 error code」**(本文件 T4 review 段)。
前端 JobsView 已按 `status/kind` 粗分类,后端出结构化 code 后前端切精确映射。**不重复立项**,在此交叉引用。

---

### **Part B 全部 admin 页需要** — T5 license + admin 路由后端缺失

**位置**:`app/api/routes/`(仅 `core.py`,无 admin 路由组)

**现状**:后端无任何 admin / account 端点。Part B 五页(§6 账户安全 MFA / §7 用户管理 /
§8 项目管理 / §9 AI 配置 / §10 审计日志)**全部无后端**,PRD 版本归属均为「T5 license + admin 路由后」。

**卡住的前端**:Part B 五页(§6–10)。**现在不能照猜的接口硬建**(违反 contract §0 不臆造)。

**触发条件**:T5 license skeleton + admin 路由落地后。**优先级**:中(GA 前必有,但排在 Part A 收口之后)。

---

## CI 基础设施

### **2026-06-02 前先验,2026-09-16 前必升** — GH Actions Node 20 弃用

**位置**:`.github/workflows/ci.yml`(actions/checkout@v4 / astral-sh/setup-uv@v3)

**现状**:CI 全部 7 个 job 都依赖这两个 action。GH Actions 的 deprecation 通告:
- **2026-06-02**:GH 强制用 Node 24 跑这些 action(action 版本不变,运行时变,**大概率兼容**)
- **2026-09-16**:这些 action 的当前 major 版本被移除(**必须升 @v5 / @v4**)

**风险**:CI 全靠这些 action 跑红线 / 测试 / cold-start。Node 24 切换小概率破坏,届时所有 jobs 同时红 → 卡住任何 PR 合并(包括"修复 CI"的 PR 本身)。

**建议办法(二选一,推荐先验)**:
- **选 A 先验**:加 `env: FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` 在 ci.yml 顶部跑一次完整 7 jobs。绿则放心,等 9 月 deadline 再升;红则立即按选 B 升
- **选 B 直接升**:`actions/checkout@v5` + `astral-sh/setup-uv@v4`,顺便解决 9 月 deadline

**触发条件**:**2026-06-02 前先做选 A** 消除不确定性(10 分钟);若验红或图省事 → 直接选 B。

**优先级**:**中**。骨架阶段 CI 是单点失败,Node 24 切换日刚好在 T7 / T5 推进期间 —— 别让基础设施在那时炸。

---

## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
