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

### **DM 列头 driver_type 显示原始 Python 类名** — dm_adapter 美化

**位置**:`app/dbclients/dm_adapter.py`(Column.driver_type 赋值处)

**现状**:DM 查询结果列头的 driver_type 显示为 `<class 'dmPython.STRING'>` 原始 repr
(2.1.0 W1 真机走查发现,PR #63 评论已记)。归一化 ColumnType(string/datetime)正确,
仅 driver_type 原样透传了 dmPython 类型对象的 repr。

**修法**:dm_adapter 侧把 dmPython 类型对象映射为干净名(STRING / TIMESTAMP / NUMBER …),
对齐 MySQL adapter 的 driver_type 风格。

**触发条件**:2.1.x 任意顺手窗口。**优先级**:低(纯显示,不影响功能)。

---

### **T7 §4 SQL 跑非 MySQL/DM 需要** — worker 仅支持 MySQL / DM

**位置**:`app/worker.py:build_database_adapter`(MySQL / DM 分发,其余 raise UnsupportedDbTypeError)

**现状(2.1.0 W1 更新)**:worker 实现 **MySQL + DM adapter**;前端 warn 兜底已落地且升级为
白名单 `{mysql, dm}`(PR #63,DM Certified 后解除其封锁)。PostgreSQL / Oracle / DB2
数据源跑 SQL 仍直接 `UnsupportedDbTypeError`,前端对这三类显示"暂不支持"提示。

**修法**:后端补 PostgreSQL / Oracle / DB2 worker adapter,每补一个方言,前端白名单
`SUPPORTED_EXECUTION_DB_TYPES`(SqlWorkspaceView.vue)同步加一项。

**触发条件**:多方言执行上线(Oracle 2.0.x / DB2 2.1.x)。**优先级**:中。

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

## 来自 1.x 切换后 dogfood(2026-06-12)

### **2.0.x 应修** — 终态 test_connection job 不应阻塞数据源删除

**位置**:`app/api/routes/core.py:_datasource_job_references`

**现状**:DELETE /datasources/{id} 的 409 引用检查把**所有** kind 的 job 计入引用,
包括已终态(failed/success)的 test_connection。后果:任何被"测试连接"过一次的
数据源永远无法删除 —— 测连本身就会创建 job。真机实锤:删除两个废弃 DM 数据源时
被各自的一条失败测连 job 挡死,只能人工授权清 job 行后才删掉。

**修法**:引用检查排除 `kind='test_connection'` 的终态 job(或仅统计
sql_query 等业务 job);保留对业务 job 的保护语义。补单测:测过连的数据源可删,
有 sql_query 历史的仍 409。

**触发条件**:下一个 2.0.x 补丁窗口。**优先级**:中(影响日常管理操作)。

---

## 来自 2.2.0 Compare 前端(2026-06-19)

### **2.2.x 应补** — Compare 结果缺专用导出端点

**位置**:`app/api/routes/core.py`(导出端点)/ 前端 CompareView 导出入口(留口未做)

**现状**:Compare-PR5 前端(#74)原计划做 4 桶结果导出,但后端
`/jobs/{id}/export` 的 `_require_exportable_source` 只接 `SQL_QUERY` job,
**没有 compare 专用导出端点**。子代理正确地未加导出按钮(避免点了就 4xx 误导用户),
在 PR 说明里标了偏差。

**修法**:后端补 compare run 导出端点(4 桶分 sheet,沿用 W2 导出的一次性 token +
公式注入防御 + 限额 + GC 通道);前端 CompareView 追加导出按钮接上。

**触发条件**:2.2.x 顺手窗口 / 用户 dogfood 提需求时。**优先级**:中(功能补全,不阻塞核心对比)。

---
## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
