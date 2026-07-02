# DataOpsStudio 2.0 Backlog

> review 过程中 surfaced 的"暂不阻塞、但要记得"项。每一条标明**最迟修复版本**和**触发条件**;到该版本前必修的标 ★ 必修。

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

## 来自 2.3.0 立项调研(2026-06-19,Scenario Lab 移期 2.6.0)

### **2.6.0 做 Scenario 时必读** — 1.x scenario 真实 DSL 测绘结论

**背景**:立项 2.3.0 时去服务器挖 1.x scenario 源码(`~/dataops-studio/app/scenarios/`),
发现 **PRD §13 推测的 DSL(steps/asserts/cleanup)是错的**,1.x 真实 DSL 强大得多。
据此决定 Scenario 移至 2.6.0 与 AI Copilot 一体(roadmap 已改,见设计稿)。

**1.x 真实 DSL = 声明式三层架构**(`app/scenarios/models.py`):
- **tables**:schema 结构。列含 8 种 generator(`uuid_short`/`random_int`/`realistic`/
  `timestamp`/`enum`/`constant`/`sequence`/`foreign_key`)。foreign_key 从另一表列值池抽样
  保证 JOIN 匹配(`references: "table.column"` + `match_rate` + `fk_distribution`)。
  支持 `dist_params`(lognormal/normal 等真实长尾分布)+ 13 个金融 domain faker provider。
- **anomalies**:故意制造偏差,6 种(`missing_rows`/`extra_rows`/`value_drift`/
  `type_mismatch`/`null_drift`/`duplicate_pk`)。target 表用 `derives_from` 继承 source + 加 anomaly。
- **workloads**:造的数据喂给谁消费,4 种(`compare_task`/`lineage_script`/`slow_query`/
  `workflow_run`),自带 `expected:` ground truth 供回归断言。

**设计精髓**:结构 deterministic / 内容 AI fill(template 控 schema,LLM 只在 `ai.fill`
白名单填业务数据)→ 这正是为何与 AI Copilot 一体做。`extra='forbid'` 拦 YAML 笔误。

**操作语义**:materialize=建表+造数(tables+anomalies);verify=跑 workloads 对 expected;
run-all=两者。cleanup: `drop_after_run` / rollback。

**源码位置(服务器)**:`~/dataops-studio/app/scenarios/`(models/materializer/orchestrator/
dialects/{mysql,oracle}/ai_filler/faker_providers/yml_importer 等 20 文件);样例 yml 在
`~/dataops-studio/config/scenarios/*.example.yml`(orders-recon 最完整)。**2.6.0 开工前先全量
读这套 + 产出 Scenario 设计稿(像 v0.3.3 Compare §2.3.1 那样),别用 PRD 推测 DSL。**

**写操作安全(用户已拍板,2026-06-19)**:Scenario 是 2.0 第一个写客户库的能力域。
边界 = **沙箱白名单 + 默认禁**:只对显式开 `allow_scenario_write` 的数据源放行
(`OperationPolicy.allow_scenario_write`,地基已预留,默认 False);生产/未标记数据源
一律拒绝执行写步骤。统一校验入口复用 sql_guard 思路。

**触发条件**:2.6.0 启动。**优先级**:高(2.6.0 核心范围,且测绘结论易随时间遗忘)。

---
## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2 与 ADR-0018 已做)。
