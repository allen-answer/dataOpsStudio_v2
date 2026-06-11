# DataOpsStudio 2.0 Backlog

> review 过程中 surfaced 的"暂不阻塞、但要记得"项。每一条标明**最迟修复版本**和**触发条件**;到该版本前必修的标 ★ 必修。

## 来自 T4 (API + middleware) review

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

### **worker DM 数据源需配 DM 客户端加密库**(2026-06-11 真实例验证发现)

**位置**:部署文档 / worker 运行环境

**现状**:PyPI `dmpython` 轮子 bundled 加密库缺传递依赖,`dlopen` 加密模块失败
(`-70089`)。真实例验证时的修法:`DM_HOME=/opt/dmdbms` +
`LD_LIBRARY_PATH=/opt/dmdbms/bin:/opt/dmdbms/bin/external_crypto_libs`,
并清掉轮子 `dmpython.libs/` 内的孤立加密库。

**修法**:on-prem 部署有 DM 数据源时,worker 进程需配上述环境;写进部署文档
(quickstart / docker-compose 的 DM 注记)。**优先级**:中(首个 DM 客户部署前)。
详见 `docs/acceptance-dm-certified.md`。

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

**优先级**:**低**。**GA 决策(2026-06-11,人拍板)**:选 C —— 不进 GA,
推迟到真有外部安全审计时再做(届时二选一方案单独立项)。触发条件由
"GA 前安全评审窗口"改为"外部安全审计启动时"。

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

**GA 决策(2026-06-11,人拍板)**:Oracle adapter **滑出 GA,进 2.0.x 后续版本**
(契约本就允许"Oracle 可滑 2.0.x");DB2 维持 Preview。

---

### **GA 发布口径** — license 以 trial-only 发布,正式签发推迟

**位置**:`app/infrastructure/license/verifier.py`(内置公钥)/ release 流程

**现状**:当前内置公钥是 T5 开发期生成的密钥对;正式 license 签发需要按设计稿 §8.8
在签发机生成真私钥(私钥永不进仓库/会话,由人保管)并替换内置公钥。

**GA 决策(2026-06-11,人拍板)**:GA 以 **trial-only**(30 天试用,无 license 文件)
口径发布;正式密钥对与签发流程推迟到首个商业交付前。

**触发条件**:首个需要正式 license 的对外交付。**优先级**:中(商业化前必做)。

---

## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
