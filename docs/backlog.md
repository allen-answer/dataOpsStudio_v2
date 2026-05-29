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

### **T5 或清理时** — BootstrapSecrets 是 plain class 而非 Protocol(契约一致性)

**位置**:`app/infrastructure/bootstrap/protocol.py`

**现状**:与配对的 `SecretStore (Protocol)` 不一致;契约 §3.3 字面写的也是 `class BootstrapSecrets:`(非 Protocol)。T3 review 时已 flag,Codex T4 加 `get_jwt_secret` 时延续 plain class 风格。

**修法**:类比之前 Step 0 把 AiGateway 从 `class` 改 Protocol —— 同样手法把 BootstrapSecrets 改 Protocol,契约 §3.3 同步更新。

**优先级**:低(plain class 实现可工作,只是与 SecretStore 风格不齐)。可在 T5 或专门清理 PR 处理。

**关联**:Step 0 AiGateway 改 Protocol 的 commit `f575503`。

---

### **可选** — 手写 JWT(HS256)→ PyJWT

**位置**:`app/api/security.py`

**现状**:Codex T4 手实现 HS256(hmac + hashlib + base64),`hmac.compare_digest` 用对了(时序安全),代码逻辑 OK。

**为什么记录**:手写 crypto 是 audit smell。PyJWT / python-jose 是 battle-tested,有维护团队,默认更严(reject `alg: none` 等)。

**修法**:`uv add pyjwt` → `security.py` 切 PyJWT API(create / decode / 处理 jwt.ExpiredSignatureError 等)。

**优先级**:低,可选。当前手写实现没漏洞(看过)。如做 GA 前安全审计时被点,即换。

---

### **T1 后随时** — `UnsupportedJobKindError` 复用语义不一致

**位置**:`app/worker.py:121` 和 `app/worker.py:240`

**现状**:同一异常类两处 raise:
- L121:`Unsupported job kind`(JobKind 不在 2.0.0 支持范围 —— 2.0.0 只 sql_query)
- L240:`Unsupported datasource db_type`(adapter factory 不支持的 DB 方言)

**问题**:两个不同语义共用一个 exception name,catch 不能区分。

**修法**:拆 `UnsupportedDbTypeError(RuntimeError)`。L240 改用新异常。

**优先级**:不阻塞,不影响功能,只影响 future error handling 清晰度。可与 T4/T5 顺手做。

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

## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
