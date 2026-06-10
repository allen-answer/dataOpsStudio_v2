# DataOpsStudio 2.0 Backlog

> review 过程中 surfaced 的"暂不阻塞、但要记得"项。每一条标明**最迟修复版本**和**触发条件**;到该版本前必修的标 ★ 必修。

## 来自 T4 (API + middleware) review

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

## 来自 DM adapter review(feat/dm-adapter-columntype)

### **DM "Certified" 宣称前必做** — DM adapter 缺真实例集成验证

**位置**:`app/dbclients/dm_adapter.py` / CI

**现状**:GH Actions 无可信 DM8 镜像(Docker Hub 仅社区镜像,GB 级 + license 受限),
本 PR 用 fake 驱动单测 + 契约测试覆盖,**没有任何真 DM 实例跑过的 hard evidence**。

**不够用**:2.0.0 范围写明 "MySQL+DM Certified"。没有真实例验证,"Certified" 站不住
(dmPython 连接参数 / callTimeout / ALL_* 视图 / DBMS_METADATA 行为均未实测)。

**修法**:在持牌环境(自托管 runner 或手动)对真 DM 实例跑一遍
`@pytest.mark.integration` 级验证(连接 / SELECT 流式 / introspection / EXPLAIN /
软取消),证据(stdout / 测试报告)归档进 PR 或 docs。

**触发条件**:对外宣称 DM Certified 前 / 2.0.0 GA 前。**优先级**:高。

---

## 来自首次云服务器 dogfood(5f27cc5,§5 真链路跑通)

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

## 来自 T7 前端 review(2δ — 前端已追平现有 5 组 endpoint 能力上限)

> **背景**:T7 phase 2δ 做数据源 §3 时逐条核对 PRD vs 后端,确认 **Part A 前端已基本追平
> 现有 5 组 endpoint(`auth/login` / `projects` / `datasources` / `sql/execute` / `jobs`)
> 能支撑的全部范围**。下列 PRD 功能**全部卡在后端缺端点 / 缺字段**,不是前端漏做 ——
> 前端侧已用 `★ 后补` 注释在对应位置标好,后端补齐即可直接接上(多数无需前端二次改)。

### **T7 §4 SQL 跑非 MySQL/DM 需要** — worker 仅支持 MySQL / DM

**位置**:`app/worker.py:build_database_adapter`(MySQL / DM 分发,其余 raise UnsupportedDbTypeError)

**现状**:worker 实现 **MySQL + DM adapter**(DM 自 feat/dm-adapter-columntype);
PostgreSQL / Oracle / DB2 数据源跑 SQL 仍直接 `UnsupportedDbTypeError`。

**卡住的前端**:SQL 工作台(§4)对非 MySQL 数据源执行 → 任务必 failed。前端**行为正确**
(正常提交 → 轮询 → 显示 failed),但用户体验是"建了 PG 数据源却跑不了"。

**修法(二选一)**:
- 后端:补 PostgreSQL(及其它方言)worker adapter
- 前端兜底(轻量先行):SQL 工作台选到非 MySQL 数据源时,执行按钮旁提示"当前仅支持 MySQL 执行",
  避免用户提交后才看到 failed(**此项前端可独立做,不阻塞后端**)

**触发条件**:多方言执行上线前 / 或先做前端 warn 兜底。**优先级**:中(2γ e2e 已实锤此限制)。

---

### **Part B 全部 admin 页需要** — T5 license + admin 路由后端缺失

**位置**:`app/api/routes/`(仅 `core.py`,无 admin 路由组)

**现状**:后端无任何 admin / account 端点。Part B 五页(§6 账户安全 MFA / §7 用户管理 /
§8 项目管理 / §9 AI 配置 / §10 审计日志)**全部无后端**,PRD 版本归属均为「T5 license + admin 路由后」。

**卡住的前端**:Part B 五页(§6–10)。**现在不能照猜的接口硬建**(违反 contract §0 不臆造)。

**触发条件**:T5 license skeleton + admin 路由落地后。**优先级**:中(GA 前必有,但排在 Part A 收口之后)。

---

## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
