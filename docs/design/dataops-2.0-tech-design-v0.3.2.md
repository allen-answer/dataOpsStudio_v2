# DataOpsStudio 2.0 技术方案 v0.3.3

> **状态**:草案,接近可作为 2.0 Charter 前置技术方案
> **最后更新**:2026-06-12
> **作者**:[你] + Claude 协作
> **目的**:作为 2.0 开发期间的活文档,每个架构决策落到此处。下一版应为精简版 Charter(3-4 页)+ 本文档作为技术方案主干。

---

## Changelog

### v0.3.3(2026-06-12)

**来源**:1.x 用户日常使用反馈 8 条,集中修订 Compare(2.2.0)与 Lineage(2.4.0)能力域。

1. **Compare 映射自动推断前移到内核规则能力**:同名/规范化相似列名 + ColumnType 兼容矩阵推断字段映射,information_schema 主键/唯一键探测推断对比主键;用户确认制替代手填制。AI 字段映射降级为"规则推不出的残余列"辅助。
2. **Compare 差异画像(diff 归因)**:compare_run 产出结构化画像(列差异率、恒定偏移/时区/精度/空串 vs NULL/trim 等系统性偏差、缺失行主键区间聚类);AI Assist 只在画像 JSON 上做 L2 自然语言归因。
3. **Compare 递归 checksum 下钻 + 采样快检**:主键区间按 8~32 段递归二分/多分,段聚合哈希一致跳过,不一致继续下钻,低于阈值才逐行;补充采样快检模式。
4. **Compare 任务建议**:跨源同名表自动配对清单,一键生成任务草稿;从血缘建议对比标注为 2.4.0 后增强,不进 2.2.0。
5. **Lineage 子图优先架构**:新增 ADR-0019。图引擎一等公民查询改为焦点表 N 跳邻域子图,按需计算/按需加载;禁止全图计算后过滤;全景图降为惰性 + 分层聚类的次要视图。验收线:焦点子图(<=3 跳)查询 P95 < 2s。
6. **基础影响分析提前到 2.4.0**:纯图反向遍历输出下游波及清单(按深度分层),不依赖 AI/运行频率;C3 保留 2.7.0,改述为"基础影响分析的 AI 增强"。
7. **解析覆盖率治理**:parse_errors 从埋在 LineageReport 字段里升级为 UI 一等公民;新增 AI 兜底解析(L2,默认可关),失败语句交 LLM 提取血缘,边标注 inferred=true + 置信级。
8. **列级血缘不可裁剪**:维持 2.4.0 第一版承诺,明确列级血缘是首版验收项,不得滑到 2.4.x。

**调研增补(2026-06-12,data-diff/hashdiff、reladiff、pt-table-checksum、Veridata、DataHub、OpenLineage、SQLMesh、LLM 血缘论文 2025)**:

1. Compare 递归 checksum 下钻采纳 data-diff/hashdiff 风格:8~32 段均分,hash 不一致再递归,低于阈值后逐行。
2. Compare 增加跨库规范化层:DB 侧执行规范化 SQL,并以 MySQL/Oracle/DM 黄金一致性测试作为 2.2.0 验收。
3. Compare 聚合哈希统一为"逐行 MD5 截 8 字节 → SUM",禁止 ORA_HASH/CRC32+BIT_XOR/CHECKSUM_AGG 等不可复现或弱哈希。
4. Compare 采样快检改为零缺陷抽样口径:输出"置信度下差异率上界",并用 PK 随机锚点替代 ORDER BY RAND()。
5. Lineage 持久化策略反转:2.4.0 起 `graph_edges` / `insert_mappings` 落 PG 边表,`sql_hash` 作为缓存键,见 ADR-0020。
6. Lineage 子图查询以 PG recursive CTE 读取边表,深度 <=5 + visited 去重,与 ADR-0019 子图优先互为前提。
7. Lineage 存储过程解析采用"语句级切分抢救":过程体先切 DML 逐条交 sqlglot,残片才走 AI 兜底。
8. Lineage 列级语义对齐 OpenLineage/DataHub:增加 `transformation`、schema-aware qualify、AI inferred → confirmed/rejected 确认状态机。

### v0.3.2(2026-05-28)

**新增章节**:

- **§9 日志与可观测性**(原 §9 商业模式占位重新启用)→ 三条管线分离设计:运行日志(structlog JSON + request_id 关联 + 强制脱敏 processor)/ 审计日志(PG audit_logs 表,合规)/ 指标(Prometheus)。强制脱敏 processor 列为 2.0.0 安全红线(防 SecretStore 被日志旁路泄密)。

**敲定此前 Open / Proposed 的决策(本轮讨论收敛)**:

1. **Hosted 云(B5/B14)** → 绑**腾讯云**,全程**自管**(CVM + Docker,复用 on-prem 形态),AI 辅助运维;腾讯云托管产品(TencentDB / COS / 云Redis / KMS)靠适配器按需接入,不进 2.0 主线。状态 Open/Tentative → **Accepted**。理由:作者已有腾讯云 CVM,自管为最低摩擦路径;自管简单架构与"AI 辅助运维"互为前提,上托管产品会超出单人 + AI 可运维范围。
2. **AI Copilot Demo(B6)** → C1-C5 具体内容 Proposed → **Accepted**。优先级 **1/5/2/4/3**(按使用频率 × 数据成熟度双线收敛)。排期:**2.6.0 = C1 + C5 基础版**(均为"读当前状态"型,day-one 满血);**2.7.0 = C2 / C4 / C5 进阶层 / C3**(均依赖历史沉淀或血缘能力域)。
3. **跨版本约束(新增)** → AI Copilot 虽 2.6.0 才做,但其依赖的"上下文接口"须在更早能力域预留:schema introspection 接口在 **2.0.0** 设计干净可复用;Scenario 模板数据结构在 **2.3.0** 预留"供 AI 读真实数据分布"接口。避免 2.6.0 返工。
4. **2.0.0 首个 adapter(B18 细化)** → **MySQL** 验证骨架(测试摩擦最低)。但 Database Gateway 接口设计须以 **Oracle/DM 为假想验证对象**(标识符大小写 / 分页语法 / cursor 语义 / 类型映射),避免 MySQL-specific 假设固化进骨架。
5. **Adapter 排期(B18 细化)** → GA Certified: **MySQL + DM**;**2.0.x**: Oracle(1.x 移植,不卡 GA);**2.1.0**: DB2(2.0.0 时 Preview)。推进顺序:**MySQL → DM → Oracle → DB2**。

**基于 1.x AS-IS 文档(`docs/legacy/V1_AS_IS.md`)核对的修正**:

6. **§2.4 推翻"lineage aspect 收敛"**:经核实,1.x lineage **不存在 aspect 概念**(代码零 `aspect` 关键字)。"12 个 aspect"是 CLAUDE.md 对代码模块数的描述,不是数据结构。此前"12 收敛到 6"是建立在错误前提上的伪命题,已删除。改为:2.0 **沿用 1.x 的 `LineageReport`(20 字段)结构**,实现按版本分批(2.4.0 先做表/列级血缘,存储过程/动态SQL后续)。"aspect"一词仅保留给资产分类(6 个:owner/pii/sla/sensitive/tag/business_term)。
7. **§5.4 迁移表按 1.x 真实结构修正**:补漏 workflow_templates;SQLite 9 张表逐表映射;audit_logs/jobs 只从 SQLite 迁(1.x 启动已把 jsonl/json 迁入 SQLite,不重复读);`.dataops_secret.key` 标为迁移**输入**(解密旧数据);lineage 解析结果不迁(重算);metadata_cache 不迁(重建)。
8. **附录 B B4 更新**:从"新写核心 6,旧 12 可读"改为"沿用 1.x LineageReport,实现分批"。

### v0.3.1(2026-05-28)

**架构级修正(采纳第四轮反馈)**:

1. **On-prem Docker 执行模型纠正**:从"同进程 ThreadPool"改为"API container + Worker container + PostgreSQL job queue"。理由:on-prem 是生产主战场,长任务不能拖死 API。
2. **Portable Runtime Supervisor**:§6 从"Managed Local PostgreSQL"扩为轻量 launcher 进程,管理 PG + API + worker 生命周期。API 进程不再直接托管 PG。定位:比 start.ps1 裸拉稳,比 Windows Service 轻。
3. **SecretStore 启动循环修正**:区分 Bootstrap Secret(连 PG 之前需要,文件系统管理)与 Application Secret(系统起来后管理,进 PG secret_refs 表)。PG 密码归 Bootstrap。
4. **ResultSet 不再持有 DbCursor**:改为 worker 流式拉取写本地 spool,DB cursor 秒级持有即关闭,UI 从 spool 分页读。避免生产库 cursor 长期占用。
5. **AdapterCapabilities 能力矩阵**:不假设所有 adapter 全功能,每个 adapter 声明支持的能力。
6. **License 失败模式软化**:invalid_signature / expired 进入 Repair Mode(只读 + 允许换 license + 备份),不再直接拒绝启动。
7. **AI Data Egress Policy**:新增 L0-L5 数据出站分级,L4/L5 默认禁在线 AI,C2/C5 demo 标 high-risk + 需 opt-in。
8. **Memory guard 拆进程**:从单一全局值拆为 api / worker / postgres / system 分别限制。

**范围调整**:

- 2.0.0 Adapter 分级:Certified(PG/MySQL/Oracle/DM)+ Preview(DB2);DB2 Certified 后置 2.1.0
- 附录 B 状态修正:B1→Deferred,B6→方向 Accepted/demo Proposed,B10→Deferred,B15→PG16 但 GA 前评估 PG17

**删除**:

- **§9 商业模式骨架整章删除**(作者决定 2.0 阶段不做商业模式设计)
- 附录 B B7(商业模式)状态改为 Deferred

### v0.3(2026-05-28)

- 元数据库回到 Postgres 统一(全形态,含 Portable bundled PG)
- AI 三层架构(Gateway / Assist / Copilot)
- Workflow = Job DAG + 节点白名单
- Lineage aspect 收敛核心 6 个 *(⚠️ 此条基于错误前提,已被 v0.3.2 推翻,见 §2.4)*
- §6 Managed Local PostgreSQL 章节
- License 离线签名模型
- 附录 B 改决策表
- 2.0.0 范围收窄到骨架版

### v0.2(2026-05-28)

- 新增 SecretStore 统一抽象(触发:发现 1.x datasources.json 明文密码漏洞)

### v0.1(2026-05-28)

- 初版

---

## 1. 总纲

### 1.1 产品定位

DataOpsStudio 2.0 是面向数据工程师与 DBA 的"数据库工作台",主要交付形态是 on-prem 私有化部署(适配金融等强内网行业),Hosted SaaS 作为辅助形态提供试用、跨团队协作、AI 增强能力。Windows Portable 摆渡部署是 on-prem 在"无法装 Docker"环境下的具体实现。

**2.0 不是功能爆炸版,而是架构、运行模型、安全边界和部署形态重做版。**

### 1.2 三种部署形态

| 维度 | Hosted SaaS | On-prem Docker | Windows Portable |
|---|---|---|---|
| 主要目标用户 | 任意团队 / 个人试用 | 能装 Docker 的客户 | 金融客户内网摆渡 |
| 部署方式 | 你的云上运维 | `docker compose up -d` | 解压 + start.ps1 → launcher |
| 元数据库 | Managed PostgreSQL | PostgreSQL container | Bundled PostgreSQL runtime |
| **Worker 进程** | **独立 worker(可扩缩容)** | **独立 worker container** | **本地 worker,由 launcher 管理** |
| **队列后端** | **Redis/RQ 或 PG queue** | **PG job queue 默认,Redis optional** | **PG job queue 或 thread backend** |
| 结果存储 | S3 / R2 / OSS | 本地 volume | 本地目录 |
| AI 能力 | hosted(Claude/GPT)默认开 | 客户配 / 默认关 | 默认关,留 provider 接口 |
| 多租户 | 是(2.7.0+) | 否(单租户多项目) | 否 |
| 鉴权 | OIDC / SSO | 本地账号 / 可接 LDAP | 本地账号 |
| 升级 | 自动 rolling | `docker compose pull && up -d` | upgrade.ps1 |
| Secret 管理 | 自管加密文件(腾讯云 KMS/SSM 按需) | 本地 master key file / docker secret | 本地 master key file |
| 进程托管 | K8s / ECS | docker compose | **launcher 进程** |

### 1.3 核心设计原则

- **单一代码库**:三种形态共享同一份业务代码,差异通过 capability flag + Infrastructure 适配器实现
- **PostgreSQL 唯一正式元数据库**:hosted / on-prem / portable 全部使用 PostgreSQL,SQLite 仅用于 unit test mock,不进入产品部署形态
- **API 与 Worker 进程隔离**:除 dev / 极简 portable 外,长任务在独立 worker 执行,不拖死 API
- **on-prem 优先**:on-prem 是金融客户唯一能接触真实数据的形态,定位为主战场
- **离线即 staging**:你自用的 portable 版本 = hosted 的天然 dogfooding / staging 环境
- **AI 可关**:AI provider 抽象,on-prem 默认关闭,客户可接私有 LLM
- **敏感数据零明文 + 零出站**:落盘永远加密(SecretStore);样本数据默认不出站到在线 AI
- **生产库零长持有**:任何 UI 交互不得导致生产数据库 cursor / 连接 / 事务长期占用
- **不向后兼容 1.x**:提供 `migrate_from_v1.py` 一次性迁移工具

### 1.4 与 1.x 的关系

- **1.x → v1-lts 分支**:只接 critical fix + 安全补丁
- **1.x 已知遗留问题**:datasources.json 明文密码(v1-lts 立刻 hotfix)
- **2.0 不向后兼容**:数据 schema / API 形态都可能不兼容
- **客户路径分流**:
  - 完全断网 + 不能装 Docker + 严苛生产环境 → 推荐继续 1.x(修完明文密码漏洞后)
  - 内网测试环境 / 能装 Docker → 推荐 2.0 on-prem Docker
  - 内网测试环境 / 不能装 Docker → 2.0 Windows portable
  - 公网 / 跨团队协作 → 2.0 hosted SaaS

### 1.5 不做清单

- ❌ Windows portable 之外的桌面客户端(Mac / Linux native binary)
- ❌ 微服务拆分
- ❌ Kubernetes Operator
- ❌ 内置 LLM(只做 provider 抽象,接外部)
- ❌ BI / 报表 / 仪表盘
- ❌ 实时协作(多人同时编辑同一 SQL tab)
- ❌ dbt / Airflow 深度集成(按需评估)
- ❌ JDBC 作为主架构(保留 JDBC Bridge 扩展接口,但不在 2.0 主线)
- ❌ SQLite 作为 2.0 正式 runtime backend
- ❌ 任何形式的明文密码 / token / API key 落盘
- ❌ 业务代码直接持有 SecretStore 之外的密码字符串
- ❌ Workflow 支持任意 shell / Python / 浏览器自动化 / 任意 HTTP URL 节点
- ❌ AI Agent 多步 Loop 在 2.0.0 落地(后置 2.6.0)
- ❌ ResultSet 长期持有生产数据库 cursor
- ❌ 样本数据值(L4)默认出站到在线 AI
- ❌ Portable launcher 做成 Windows Service / 自动重启 / 健康探针级运维(过度工程)
- ❌ **2.0 阶段商业模式设计**(定价 / 套餐 / license 商业策略,推迟到 GA 前)

### 1.6 待定 / 待讨论

- AI Agent Loop 在 2.6.0 时是否引入框架
- 1.x 用户迁移到 2.0 的具体路径与时机

---

## 2. 技术架构

### 2.1 整体分层

```
┌──────────────────────────────────────────────────────────────┐
│                      前端 SPA (Vue 3 + TS)                    │
│  ┌──────────────┬──────────────┬──────────────┬──────────┐  │
│  │ SQL Workspace│   Compare    │ Scenario Lab │ Lineage  │  │
│  ├──────────────┴──────────────┴──────────────┴──────────┤  │
│  │           AI Copilot (横切面板,所有能力域可用)         │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
                              ↓ HTTPS / SSE
┌──────────────────────────────────────────────────────────────┐
│   API Process (FastAPI)                                       │
│   Governance Layer (middleware,必经):                        │
│   AuthN / License Check / AuthZ / Operation Policy /         │
│   SQL Guard / Resource Guard / Audit / Rate Limit            │
│   职责:接请求 / 鉴权 / 创建 Job / 查状态 / 读结果 spool       │
└──────────────────────────────────────────────────────────────┘
                              ↓ enqueue (PG jobs 表)
┌──────────────────────────────────────────────────────────────┐
│   Worker Process(es)                                          │
│   职责:消费 Job / 实际 DB 访问 / 流式写结果 spool / AI 调用    │
│   ┌────────────────┬──────────────────┬──────────────────┐   │
│   │ Job Executor   │ Database Gateway │   AI Runtime     │   │
│   │ • sql_query    │ • execute_select │ • Gateway        │   │
│   │ • compare_run  │ • explain        │ • Assist         │   │
│   │ • workflow_run │ • introspect_*   │ • Copilot        │   │
│   │ • lineage_*    │ • stream→spool   │   (egress policy)│   │
│   │                │ • kill_query     │                  │   │
│   └────────────────┴──────────────────┴──────────────────┘   │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│        Infrastructure (可替换实现,形态差异收敛于此)            │
│  MetadataStore   (PostgreSQL 唯一)                            │
│  JobBackend      (PG queue / Redis-RQ / ThreadPool)           │
│  ResultStore     (LocalFS / S3) ← ResultSet spool 落这里      │
│  SecretStore     (Application Secret: LocalFile/Env/KMS)      │
│  BootstrapSecrets (master key / pg pwd / license,文件系统)    │
│  CacheStore      (in-memory / Redis)                          │
│  LicenseStore    (LocalFile signed-payload)                   │
└──────────────────────────────────────────────────────────────┘
```

**关键变化(v0.3.1)**:API 进程不再执行长任务。API 只负责接请求、鉴权、入队 Job、查状态、读已 spool 的结果。Worker 进程负责实际 DB 访问和结果落盘。

### 2.2 进程与执行模型(v0.3.1 重写)

#### 2.2.1 各形态进程拓扑

| 形态 | 进程拓扑 | JobBackend |
|---|---|---|
| Hosted | API pods + Worker pods(独立扩缩容) | Redis/RQ 或 PG queue |
| On-prem Docker | api container + worker container + postgres container | PG job queue(默认),Redis optional |
| Windows Portable | launcher → postgres + api + worker(独立进程,同机) | PG job queue 或 thread backend |
| Dev | 单进程(thread backend) | ThreadPool |

#### 2.2.2 PostgreSQL Job Queue(取代对 Redis 的硬依赖)

既然 PostgreSQL everywhere,用 PG 自身实现轻量任务队列,on-prem 不必引入 Redis:

```sql
-- worker 抢任务
UPDATE jobs
SET status = 'running', worker_id = :worker_id, started_at = now()
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'pending'
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

`FOR UPDATE SKIP LOCKED` 保证多 worker 并发抢任务不冲突。适用于:

- on-prem Docker:1 个 worker container(可扩到 N 个)
- portable:1 个 worker 进程
- hosted:可用 PG queue 起步,量大再切 Redis/RQ

**JobBackend 抽象**:

```python
class JobBackend(Protocol):
    def enqueue(job: Job) -> None: ...
    def claim_next(worker_id: str) -> Optional[Job]: ...
    def complete(job_id: str, result_ref: ResultRef) -> None: ...
    def fail(job_id: str, error: str) -> None: ...
    def request_cancel(job_id: str) -> None: ...
    def heartbeat(job_id: str, worker_id: str) -> None: ...

class PostgresJobBackend(JobBackend): ...   # 默认
class RedisRqJobBackend(JobBackend): ...     # hosted scale-out optional
class ThreadPoolJobBackend(JobBackend): ...  # dev / 极简 portable
```

#### 2.2.3 Worker 心跳与故障恢复

- worker 定期 `heartbeat` 更新 `jobs.last_heartbeat`
- 调度器扫描:`status=running` 且 `last_heartbeat` 超时 → 标记 failed 或重新入队。2.0.0 使用全局 `job_default_max_retries`,per-job retry_policy 后置到 workflow 版本正式定义。
- portable 单 worker:launcher 监控 worker 进程存活,崩溃则记录日志(2.0.0 不自动重启,见 §6)

**已知限制 / Backlog**:

- 2.0.0 不做独立 heartbeat 线程。慢首行 OLAP 查询期间可能长时间没有安全点心跳,因此默认 `worker_heartbeat_timeout` 设为 600 秒;产品取舍是"避免误杀合法慢查询"优先于"worker 死亡后更快恢复"。见 ADR-0018。
- T4 前必修:worker 执行 SQL 时取消检查需改为每 N 行检查或按秒缓存(N 默认 5000),避免百万行结果触发百万次 PG `is_cancel_requested` 查询。
- Backlog:将 worker 的 `UnsupportedJobKindError` 与 adapter 工厂的 unsupported db type 错误拆分为 `UnsupportedDbTypeError`。

### 2.3 横切层(必经路径)

任何 HTTP 请求进入业务逻辑之前,API 进程内必经以下层(按顺序):

```
HTTPS Request
    ↓
RequestId Middleware       (生成 request_id)
    ↓
AuthN                      (JWT / Session 校验)
    ↓
License Check              (cached license state;非 valid 时按 §8 限制)
    ↓
Rate Limit                 (per-IP / per-user)
    ↓
Audit Log                  (记录请求开始)
    ↓
Business Handler
    ├── Project AuthZ       (用户能访问该项目吗)
    ├── Datasource AuthZ    (用户能访问该 datasource 吗)
    ├── Operation Policy    (该操作在该环境下允许吗)
    ├── SQL Guard           (SQL 是否合规)
    ├── Resource Guard      (磁盘 / 内存 / 任务并发是否超限)
    └── 创建 Job 入队        (不在 API 进程执行长任务)
    ↓
Audit Log                  (记录请求结束)
    ↓
HTTPS Response (返回 job_id,前端轮询/SSE 拿进度)
```

实际 DB 访问发生在 Worker 进程的 Database Gateway 内:

```
Worker claim Job
    ↓
SecretStore.reveal_secret(ds.password_ref)   (Application Secret)
    ↓
建连 → 执行 → 流式写 spool → 关连接/cursor
    ↓
JobBackend.complete(job_id, result_ref)
```

**核心约束 1**:业务模块不允许直接访问 Database,必须经过 Database Gateway。CI lint 检查 `import psycopg / oracledb / pymysql / dmPython` 只允许出现在 `app/dbclients/` 和 `app/infrastructure/`。

**核心约束 2**:业务模块不允许直接持有密码字符串,必须通过 SecretStore。CI lint 检查直接读取 `ds["password"]` → fail。

### 2.3.1 Compare:规则智能化优先,AI 只补残余(v0.3.3 修订)

> **v0.3.3 调整**:1.x 日常使用反馈表明,Compare 最大摩擦不是"有没有 AI 按钮",而是建任务前要手填主键/字段映射、全量逐行慢、跑完后只知道有差异不知道为什么。2.2.0 范围不滑期,但智能化重心改为**确定性规则内核优先**,AI 只在规则无法覆盖的残余问题上降级辅助。

**2.2.0 必做能力**:

1. **映射自动推断(规则版,非 AI 内核)**:
   - 字段映射:同名匹配、规范化相似列名匹配(大小写/下划线/常见前后缀/中英文别名表)、ColumnType 兼容矩阵过滤。
   - 主键推断:优先 information_schema 主键;无主键时探测唯一键/唯一索引;仍无候选时给出"需人工选择"状态。
   - 交互原则:用户确认制,不是手填制。系统生成候选映射和主键草稿,用户只需确认/调整/忽略。
   - AI Assist 字段映射重新定位为"只处理规则推不出的残余列",输入不包含样本值时为 L2;需要真实样本值的学习型映射仍属 C2,保留到 2.7.0 且按 L4 opt-in 管理。

2. **差异画像(diff attribution profile)**:
   - compare_run 除 4 桶结果外,额外产出结构化 `diff_profile` JSON。
   - 指标包括:每列差异率、每列 NULL/空串/trim 后变化、数值精度/舍入差异、时间字段时区/日期截断差异、恒定偏移(如金额整体 +1、日期整体 +8h)、缺失行按主键区间聚类。
   - AI Assist 差异归因只读取 `diff_profile` JSON,生成自然语言解释/排查建议;可降级,不能影响 compare_run 成功与否。

3. **递归 checksum 下钻(hashdiff,替代固定分块预筛)**:
   - 对主键区间按 bisection factor 均分 8~32 段;两侧段内聚合哈希一致则整段跳过,不一致段递归再分。
   - 当段内估算行数低于 bisection threshold(默认约 16K)后,停止递归并切换为逐行拉取比对。
   - 理由:data-diff/hashdiff 类算法在差异稀疏时成本逼近一次 `count(*)`;固定分块在最常见的"全表一致"场景会制造大量小查询,反而浪费。
   - 递归下钻参数纳入 RunLimits: `bisection_factor`、`bisection_threshold`、`max_bisection_depth`;`compare_batch_size` 继续作为逐行阶段批量拉取大小。
   - checksum 是预筛优化,不是结果真值;最终差异仍以逐行比较为准。

4. **跨库规范化层(成败关键)**:
   - client 为每个 adapter 生成"规范化 SQL",在 DB 侧执行并产出可跨库复现的行级规范化值。
   - 规范化内容包括:数值定点定标、时间统一精度、NULL 哨兵(CONCAT_WS + ISNULL 位图思路)、显式字符集/排序规则、字符串 trim/case 策略与 CompareRules 对齐。
   - 2.2.0 验收必须产出规范化 spec 文档,并提供 MySQL / Oracle / DM 三库黄金一致性测试:同一逻辑行在三库计算出的行哈希必须相等。
   - 跨库规范化失败时,任务必须显式降级到 client 侧哈希/逐行模式,不能静默混用引擎默认 hash。

5. **聚合哈希统一为"逐行 MD5 截 8 字节 → SUM"**:
   - 每行先基于规范化列值计算 MD5,取高/低 8 字节转整数,段级聚合使用 `SUM(row_hash64)`;SUM 是可交换聚合,三库均可表达。
   - 明确禁止用 ORA_HASH、CRC32+BIT_XOR、CHECKSUM_AGG 等引擎私有或弱哈希做跨库真值:ORA_HASH 跨库不可复现,CRC32 碰撞率高,CHECKSUM_AGG 语义/碰撞不可控。
   - DM8 路径优先评估 `DBMS_CRYPTO.HASH(MD5)`;标注性能 PoC 为 2.2.0 前置项。若过慢,DM 端降级为 client 侧流式哈希。

6. **采样快检定量化(零缺陷抽样)**:
   - 抽样快检只做风险估计,不承诺全等。产品固定话术:"在 X% 置信度下,差异率上界 < p"。
   - 若抽 n 行全一致,则 `1 - alpha` 置信度下差异率 `< p`,近似 `n = ln(alpha) / ln(1 - p)`。
   - 锚点数字:抽 300 行全一致约等于 95% 置信度下差异率 < 1%;抽 3000 行全一致约等于 95% 置信度下差异率 < 0.1%。
   - 抽样实现用 PK 随机锚点/区间采样,禁止 `ORDER BY RAND()` 之类全表随机排序。
   - 行级下钻借鉴 Veridata:PK 取真值,非键列只拉规范化行哈希;只有需要展示 diff 单元格时才取非键列明细,减少二次回查。

7. **对比任务建议**:
   - 跨源同名表/规范化同名表自动配对,展示候选清单,支持一键生成 CompareTask 草稿。
   - 迁移场景优先:source/target 数据源选择后,系统给出"可能应对比的表"列表,减少逐表新建任务成本。
   - "从血缘建议对比"标注为 2.4.0 后增强:等 lineage 能提供可靠上下游/影响关系后再接入,不进入 2.2.0 验收。

**验收线**:

- 新建对比任务默认进入"自动推断结果确认"状态,不是空白手填表单。
- 用户可以在不调用 AI 的情况下完成常见同构/迁移对比任务配置。
- compare_run 结果必须包含 `diff_profile` 结构化摘要;AI 解释失败不能导致任务失败。
- 递归 checksum 下钻必须可关闭,且关闭后结果与开启后一致。
- 跨库规范化黄金测试(MySQL/Oracle/DM)必须证明同一逻辑行三库哈希相等。

### 2.4 Lineage:沿用 1.x LineageReport,子图优先实现(v0.3.3 修订)

> **v0.3.2 重大更正**:此前版本写"1.x 有 12 个 lineage aspect,2.0 收敛到 6 个核心 aspect"——**这个前提是错的**(经 `docs/legacy/V1_AS_IS.md` §9 核实)。1.x lineage **不存在 aspect 概念**(`grep aspect app/lineage/` 零结果)。"12 个 aspect"是 CLAUDE.md 对**代码模块数**的描述性叫法,不是数据结构。故"收敛"无的放矢,已删除。
>
> **v0.3.3 补充**:本次不改变 `LineageReport` 20 字段沿用和按版本分批实现的结论;但**反转 2.0 lineage 持久化策略**(见 ADR-0020)。1.x 的慢与不可读主要来自"先算全图再让人过滤",2.0 子图优先需要可查询的边表支撑。

**1.x 真实情况**(AS-IS §9):

- lineage 解析产出是**一个** `LineageReport` Pydantic 模型(`app/models/lineage.py`),含 **20 个字段**,一次解析整体产出,不是 N 个可独立开关的 aspect。
- "aspect"一词在 1.x 只属于**资产分类系统**(asset_aspects:owner/pii/sla/sensitive/tag/business_term 共 6 个,存 SQLite,见 §5.1)。这与 lineage 解析**零关系**,两套独立。
- 1.x lineage 解析结果**不持久化**——每次跑重算,只在 workload(scenario / workflow 节点 / batch 导出)时落 JSON。该结论仅描述 1.x 现状,不再作为 2.0 持久化策略。

**2.0 策略:沿用 + 分批实现,不重新发明**

1. **数据结构**:2.0 lineage 复用 1.x 的 `LineageReport` schema(20 字段,已被 12 个 lineage 测试验证),不重新定义 aspect 体系。字段保持(Pydantic `extra="allow"` 可透传新字段)。这 20 字段是 1.x 踩 Oracle 存储过程 / 动态 SQL / PL/SQL 变量真实坑磨出来的,主动归纳成"6 个"会丢信息,违反"业务规则信 1.x"原则。

   LineageReport 20 字段分层(详见 AS-IS §9.4.3):
   - 表/列层:`tables` / `columns` / `insert_mappings` / `aliases`
   - 写操作层:`target_summary`(含 refresh_mode) / `table_roles`
   - SQL 子句:`joins` / `filters` / `group_by` / `unions`
   - 动态 SQL / 存储过程:`dynamic_sql_count` / `dynamic_sql_segments` / `procedure_segments`
   - 变量:`variables`(PL/SQL)
   - 图:`graph_edges` / `graph_groups`
   - 警告:`parse_errors` / `warnings`
   - 详情:`statements` / `semantic_lineage` / `report`
   - AI 字段(`ai_enrichment` / `ai_inferred`)由 service 层注入,envelope 透传
   - v0.3.3 增补:`insert_mappings` 增加 `transformation` 字段,对齐 OpenLineage column lineage facet:顶层 `DIRECT` / `INDIRECT`,并保留 subtype(如 `AGGREGATION` / `FILTER` / `JOIN` / `CAST` / `EXPRESSION`)。

2. **实现按版本分批(这才是真正该"精简"的)**:精简的是"2.0 哪个版本先做哪部分功能",不是砍数据字段。
   - 2.0.0 ~ 2.3.x:**不做 lineage**(lineage 能力域排在 2.4.0)
   - 2.4.0:第一版做核心——表级血缘(`graph_edges` / `tables`)+ **列级血缘(`insert_mappings` / `columns`,不可裁剪)** + 解析错误治理(`parse_errors`)+ 子图优先图查询 + 基础影响分析 + 边表持久化
   - 2.4.x / 后续:复杂部分——存储过程深度解析(`procedure_segments`)、动态 SQL 推断(`dynamic_sql_*`)、PL/SQL 变量(`variables`)、更复杂的 AI 推断(`ai_inferred`)。存储过程优先做"语句级切分抢救":PL/SQL 过程体按语句切分提取 DML 逐条交 sqlglot 解析,只有无法切分/无法解析的残片才进入 AI 兜底,降低 LLM 调用量。
   - 字段全程保留,只是实现有先后

3. **持久化策略(v0.3.3 反转,ADR-0020)**:
   - 废除 2.0 "不持久化、每次重算"策略。2.4.0 起 `graph_edges` / `insert_mappings` 落 PostgreSQL 边表(如 `lineage_edges` / `lineage_column_edges`),供子图查询、影响分析、覆盖率治理复用。
   - `sql_hash` 作为解析缓存键:同一 datasource/dialect/schema 上语句未变更则复用已持久化边;语句变更才重解析。R3 注:`sql_hash` 属非 secret 指纹用途的 `hashlib`,不受加密原语路径红线限制。
   - 子图 N 跳查询走 PostgreSQL recursive CTE,深度上限 <=5,使用 visited 去重避免环;这与 ADR-0019 子图优先互为前提。
   - `LineageReport` 20 字段降级为 API 输出/导出 envelope,结构保持不变;其中确定性边来自 PG 边表,AI/覆盖率等补充字段继续 envelope 透传。
   - `migrate_from_v1.py` **仍不迁 1.x lineage 解析结果**(1.x 本就不持久化,重算即可),只迁 asset_aspects 那两张表(见 §5.4)。2.0 内部产生的边表数据由 2.4.0 起持久化,与 1.x 迁移无关。

4. **资产分类 aspect 不变**:asset_aspects 的 6 个分类(owner/pii/sla/sensitive/tag/business_term)沿用 1.x 的动态可扩展标签机制(yml schema + SQLite KV),迁移照迁。

5. **子图优先图引擎(ADR-0019)**:
   - 一等公民查询是"焦点表 N 跳邻域子图",默认 N<=3,从焦点表按方向/深度按需遍历、按需加载。
   - **禁止实现路径**:先计算/渲染全图,再在前端或后端过滤出焦点邻域。该路径复刻 1.x 慢和不可读的根因。
   - 全景图降级为次要视图:惰性加载 + 分层聚类 + 明确的"可能很大"提示,不能成为默认入口。
   - 验收线:焦点子图(<=3 跳)查询 P95 < 2s;结果需包含节点/边数量、截断提示、深度分层。

6. **基础影响分析提前到 2.4.0**:
   - 输入:焦点表/列 + 方向(默认 downstream) + 最大深度。
   - 输出:纯图反向遍历得到的下游波及清单,按 depth=1/2/3 分层,标出路径、直接/间接依赖、是否截断。
   - 不依赖 AI、不依赖运行频率、不依赖 owner/业务权重;这使其能随 2.4.0 图能力一起上线。
   - C3 保留到 2.7.0,但定义改为"基础影响分析的 AI 增强":加入运行频率、负责人、历史变更/事故数据后做加权排序和自然语言建议。

7. **解析覆盖率治理**:
   - `parse_errors` 不再只是 LineageReport 里的一个字段,而是 2.4.0 UI 一等公民:展示哪些语句没解析出来、失败原因、占比、按文件/语句类型聚合。
   - 新增 AI 兜底解析(L2,默认可关):确定性解析失败的语句可交 LLM 提取表/列血缘,写入 `ai_inferred` 或等价 envelope 透传字段。
   - AI 推断边必须标注 `inferred=true`、`confidence`、`reason/source_kind`;前端视觉区分确定性解析结果与 AI 推断结果。
   - AI 兜底结果进入状态机:`inferred` → `confirmed` / `rejected`。人工确认前不得与确定性结果同权重参与影响分析;confirmed 后可提升参与权重,rejected 必须从默认图/影响分析中排除但保留审计轨迹。
   - AI 兜底失败不能覆盖或删除 deterministic parse_errors;只能补充。

8. **列级血缘不可裁剪**:
   - 2.4.0 第一版必须包含列级血缘 `insert_mappings` / `columns` 展示与导出,不允许以"先只做表级图"替代。
   - 若方言/语句暂不能解析列级,必须在覆盖率治理中明确记为 parse_error / unsupported / low confidence,而不是静默降级。
   - 解析必须 schema-aware:从 PG 元数据库注入表结构给 sqlglot qualify,用于 `SELECT *` 展开与多表裸列归属。DataHub 类实践显示 schema-aware 列级精度显著高于 naive 解析,因此这是 2.4.0 验收项而非优化项。

### 2.5 Job 抽象(统一执行模型)

**1.x 痛点**:5 套 execution 并存,cancel / retry / owner / audit 各搞各的。

**2.0 统一**:所有长任务都是 `Job`,`kind` 字段区分类型。

```python
class Job:
    id: str
    kind: Literal[
        "sql_query", "sql_explain", "compare_run", "export_excel",
        "scenario_materialize", "scenario_run_all", "workflow_run",
        "ai_assist_call", "ai_copilot_run", "lineage_analyze",
    ]
    status: Literal["pending", "running", "success", "failed", "cancelled", "timeout"]
    owner_user_id: str
    project_id: str
    datasource_ids: list[str]
    priority: int
    timeout_seconds: int
    resource_profile: ResourceProfile
    result_ref: Optional[ResultRef]
    audit_id: str
    worker_id: Optional[str]
    last_heartbeat: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    cancel_requested: bool
    cancel_reason: Optional[str]
    error: Optional[str]
    retry_count: int
    payload: dict
    parent_workflow_run_id: Optional[str]
```

### 2.6 SQL Workspace 与 ResultSet(v0.3.1 重写)

**1.x 痛点**:查询超 1000 行被截断;同窗口不能重复查同一表。

**v0.3 遗留风险(本版修正)**:ResultSet 持有 `DbCursor` + TTL 30 分钟 → 生产库 cursor / 连接 / 事务被 UI idle 长期占用。

#### 2.6.1 修正后的数据流

```
用户在 SQL Workspace 执行 SQL
    ↓
创建 Job(kind=sql_query),入队
    ↓
Worker claim → 建连 → 执行
    ↓
Worker fetchmany 流式拉取 → 写本地 spool(ResultStore)
    ↓
拉满阈值(如 spool 上限)或拉完 → 关闭 cursor / 还连接   ← DB cursor 秒级持有
    ↓
Job 完成,result_ref 指向 spool
    ↓
前端 fetch_range(offset, limit) 从 spool 读,不碰生产库
```

#### 2.6.2 数据模型

```python
class SqlExecution(Job):
    """kind=sql_query,一次 SQL 执行事件"""
    sql: str
    sql_hash: str
    result_set_id: Optional[str]

class ResultSet:
    """一次执行的结果,数据在本地 spool,不持有 DB cursor"""
    result_set_id: str
    execution_id: str
    storage_ref: ResultRef          # 指向本地 spool(parquet / arrow)
    columns: list[Column]
    loaded_rows: int                # worker 已 spool 的行数
    total_rows: Optional[int]       # 已知则填,未知 None
    state: Literal["streaming", "complete", "failed", "closed"]
    created_at: datetime

    def fetch_range(offset: int, limit: int) -> list[Row]: ...  # 读 spool
    def request_count_total() -> int: ...                        # 用户主动触发
```

#### 2.6.3 "边拉边看"

- worker 一边 `fetchmany` 一边写 spool,实时更新 `loaded_rows`
- 前端轮询(或 SSE)`loaded_rows`,virtual scroll 从 spool 读已加载部分
- 用户体验等同于流式,但生产库连接不被 UI idle 绑住

#### 2.6.4 资源约束

```yaml
sql_workspace:
  spool_max_rows: 1000000          # 单 ResultSet spool 行数上限
  spool_max_bytes: 536870912       # 512MB,超出停止拉取并标记 truncated
  cursor_max_hold_seconds: 300     # DB cursor 最长持有(防超大表无限拉)
  initial_fetch_rows: 500          # 前端首屏
  result_ttl_days: 7               # spool 文件保留期
  max_active_resultsets_per_console: 3
```

每次 execute 产生新 ResultSet,永不去重。同 console 超 3 个 active ResultSet,LRU 清理 spool。

### 2.7 AI 三层架构 + 数据出站策略

#### 2.7.1 三层划分

```
AI Copilot   (2.6.0+,hosted 主卖点)   多步推理 + tool calling + memory
    ↓ uses
AI Assist    (2.1.0+,常驻基础体验)     纯文本输入输出,无状态,无 memory
    ↓ uses
AI Gateway   (2.0.0,基础设施)          provider 抽象/脱敏/审计/budget/egress 检查
```

#### 2.7.2 AI Gateway(2.0.0 必做)

所有 AI 出站请求必经此层。强制能力:

1. **Egress 检查**:按 §2.7.5 数据分级,拦截禁止出站的内容
2. **脱敏**:`RedactionPolicy` 控制 SQL / 表名 / 列名 / 样本数据脱敏
3. **审计**:每次调用落 `ai_usage` 表(user / project / purpose / tokens / cost / egress_level)
4. **Budget**:per-user / per-project / per-tenant 的 token / cost 上限
5. **Provider 抽象**:on-prem 客户可配私有 LLM
6. **关闭开关**:on-prem / portable 默认关,可全局或 per-feature 关
7. **失败降级**:LLM 不可用时业务功能仍可用(无 AI 增强)

```python
class AiGateway:
    def complete(prompt: str, context: AiContext, options: AiOptions) -> AiResponse: ...
    def stream_complete(...) -> Iterator[AiChunk]: ...

class AiContext:
    items: list[ContextItem]    # 每个 item 带 egress_level

class ContextItem:
    content: str
    egress_level: EgressLevel   # L0-L5,见 §2.7.5
    redacted: bool
```

#### 2.7.3 AI Assist(2.1.0+,常驻基础功能)

纯文本输入输出,无 memory,无 tool calling。

| 能力 | 输入 | 输出 | 上线 | 最高 egress |
|---|---|---|---|---|
| SQL 执行计划解释 | EXPLAIN 文本 | 解释 + 风险等级 | 2.1.0 | L1 |
| SQL 改写建议 | 慢 SQL + schema 摘要 | 改写 SQL + 风险 | 2.1.0 | L3 |
| 数据库错误翻译 | 报错信息 | 中文解释 + 排查方向 | 2.1.0 | L1 |
| Compare 残余字段映射建议 | 规则推断未覆盖的源/目标列名列表 | 候选映射 + 置信度 | 2.2.0 | L2 |
| Compare 差异归因解释 | `diff_profile` 画像 JSON | 自然语言归因 + 排查建议 | 2.2.0 | L2 |
| 血缘结果文字解释 | sqlglot 解析结果 | 自然语言概述 | 2.4.0 | L2 |
| Lineage AI 兜底解析 | 确定性解析失败的 SQL 片段 | inferred 血缘边 + 置信级 + 原因 | 2.4.0 | L2 |

输出永远是建议 / 解释,**不直接动数据库**。复制 / 应用必须用户显式触发。不是 hosted 卖点,是产品基础体验。

#### 2.7.4 AI Copilot(2.6.0+,hosted 商业差异化)

> AI 的价值不在"会不会写 SQL",而在"知不知道你的数据"。Hosted Copilot 卖的是上下文优势,不是模型优势。

5 个差异化场景(**Accepted**;优先级与排期按使用频率 × 数据成熟度双线收敛):

| 优先级 | Demo | 内容 | 上线 | 最高 egress | 风险 |
|---|---|---|---|---|---|
| 1 | C1 Schema-Aware SQL 生成 | 自然语言 → 用真实 schema 生成 SQL | **2.6.0** | L2 | 中 |
| 2 | C5 Scenario 智能脚手架(基础版) | schema + **当前真实数据** → 生成 scenario.yml | **2.6.0** | **L4** | **高** |
| 3 | C2 Compare 字段映射学习 | 两边 schema + **20 行样本** + 历史映射决策 | **2.7.0** | **L4** | **高** |
| 4 | C4 慢 SQL 根因诊断 | EXPLAIN + 统计 + 历史基线 → 根因排序 | **2.7.0** | L3 | 中 |
| — | C5 进阶层 | 加入历史异常模式库(在基础版上叠加) | **2.7.0** | **L4** | **高** |
| 5 | C3 Lineage Impact 分析 | 2.4.0 基础影响分析 + 运行频率 + 负责人 → 加权影响清单 | **2.7.0** | L2 | 中 |

**排期依据**:

- **2.6.0(工具刚发布,无历史)只做"读当前状态"型**:C1(纯 schema,day-one 满血)、C5 基础版(读当前真实数据造测试数据,不依赖历史异常模式库)。避免"AI 说根据你的历史…用户说我没历史"的空壳尴尬。
- **2.7.0(积累数月真实使用)做"读历史"型**:C2(需映射历史;不同于 2.2.0 规则推断后的残余列 L2 建议)、C4(需运行基线)、C5 进阶(需异常模式库)、C3(需 2.4.0 基础影响分析 + 运行频率数据)。
- **C3 排末位**:血缘是低频深挖场景(非天天用),价值密度低,做晚痛感小。

**C2 / C5(L4)涉及真实样本数据值,标记 high-risk**:

- hosted:用户显式 opt-in 才发送,且脱敏 + 审计
- on-prem / portable:默认禁止,需管理员开启 + 配私有 LLM

**跨版本依赖(重要)**:C1 要 2.6.0 满血,则 **2.0.0 的 Database Gateway introspection 接口(schema/列/外键)必须设计干净可复用**,不能只够前端用;C5 基础版要 2.6.0 满血,则 **2.3.0 做 Scenario Lab 时,Scenario 模板数据结构必须预留"供 AI 读真实数据分布"的接口**,不能等 2.6.0 回头改 schema。即:AI 上下文接口要在更早能力域埋好,见 §10 跨版本约束。

#### 2.7.5 AI Data Egress Policy(v0.3.1 新增)

所有进入 AI Gateway 的内容按敏感度分级:

| 级别 | 示例 | 默认在线 AI 可发送 |
|---|---|---|
| L0 | 产品说明、错误码字典 | ✅ |
| L1 | SQL 模板、执行计划(不含表注释) | ✅ 可配置 |
| L2 | 表名、列名、schema 结构 | ⚠️ 需管理员允许 |
| L3 | SQL 原文、业务过滤条件 | ⚠️ 需脱敏 + 审计 |
| L4 | 样本数据值 | ❌ 默认禁止,需显式 opt-in |
| L5 | 密码、token、身份证、手机号等 | ❌ 永久禁止 |

**形态默认策略**:

```yaml
ai_egress_policy:
  hosted:
    max_auto_level: L1          # L2+ 需 admin / 用户 opt-in
    l4_requires_optin: true
    l5_blocked: true
  onprem_docker:
    max_auto_level: L0          # 默认最保守,客户按需放开
    l4_requires_optin: true     # 且需配私有 LLM
    l5_blocked: true
  portable:
    max_auto_level: L0
    l4_allowed: false           # 默认完全禁止
    l5_blocked: true
```

L5 在任何形态、任何配置下永久禁止出站。Gateway 内置 L5 模式检测(密码字段名 / 身份证正则 / 手机号正则等),命中即拦截并审计。

#### 2.7.6 AI 部署形态差异

| 能力 | hosted | on-prem Docker | portable |
|---|---|---|---|
| AI Gateway | ✅ 必需 | ✅ 必需(默认关) | ✅ 必需(默认关) |
| AI Assist | ✅ 默认开 | ⚠️ 客户配 provider | ⚠️ 客户配 provider |
| AI Copilot C1/C3/C4(非样本类) | ✅ 主卖点 | ⚠️ 接私有 LLM | ❌ |
| AI Copilot C2/C5(L4 样本类) | ⚠️ opt-in | ⚠️ opt-in + 私有 LLM | ❌ |
| 跨会话向量记忆 | ✅ pgvector | ⚠️ 客户开 | ❌ |

#### 2.7.7 AI Agent Loop 决策

2.0.0 ~ 2.5.x 不做 Agent Loop。2.6.0 做 Copilot 时再决定自写 vs 框架(当前倾向自写极简 Loop:≤3 step + tool allowlist + timeout + token budget + audit)。**charter 不写死框架选型**。

### 2.8 Workflow = Job DAG

**核心约束**:Workflow 不是低配 Airflow,只编排 DataOpsStudio 内置 Job。

#### 2.8.1 节点白名单

```python
ALLOWED_WORKFLOW_NODE_KINDS = {
    "sql_query", "sql_explain", "compare_run",
    "scenario_materialize", "scenario_run_all", "lineage_analyze",
    "export_excel", "notify", "sleep", "branch",
}
```

**禁止节点(2.0 主线永不开放)**:shell / system command、Python script / 任意代码、浏览器自动化、任意 HTTP request(带 secret / 任意 URL)、任意 DDL/DML 直接执行。`notify` 只允许引用 Workflow 现有已配置的 webhook、企业微信(WeCom)或邮件目标。

#### 2.8.2 数据模型与 API

```python
class WorkflowSpec:
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    schedule: Optional[CronSchedule]

class WorkflowNode:
    id: str
    job_kind: AllowedJobKind
    payload: dict
    retry_policy: RetryPolicy
    timeout_seconds: int
    on_failure: Literal["abort", "continue", "branch"]

class WorkflowEdge:
    source: str
    target: str
    trigger: Literal["success", "failure"] = "success"
    when: Optional[str] = None
    is_default: bool = False
```

2.4.x 开放 `notify/sleep/branch` 后,实际支持 8 种节点;Scenario 两种仍等 2.6.0。
条件边按声明顺序 first-match + 唯一 default。节点 payload / `when` 允许
`${nodes.<ancestor>.<field>}` 安全输出引用,但只开放按 kind 定义的标量元数据,
不开放结果行、ResultRef URI、SecretRef 或任意 metadata。

```
POST   /api/workflows
GET    /api/workflows/{id}
POST   /api/workflows/{id}/runs
GET    /api/workflow-runs/{run_id}
POST   /api/workflow-runs/{run_id}:cancel
```

WorkflowRun 自身是 Job(`kind=workflow_run`),DAG 每个节点也是 Job(`parent_workflow_run_id` 回指)。统一调度 / 审计 / cancel。

### 2.9 Database Gateway 与能力矩阵(v0.3.1 修正)

```python
class DatabaseAdapter(Protocol):
    capabilities: AdapterCapabilities    # 声明支持哪些能力

    def execute_select(sql: str, params: dict) -> Iterator[Row]: ...  # 流式
    def explain(sql: str) -> PlanNode: ...
    def stream_rows(sql: str) -> Iterator[Row]: ...
    def test_connection() -> bool: ...
    def list_schemas() -> list[Schema]: ...
    def list_tables(schema: str) -> list[Table]: ...
    def list_columns(schema: str, table: str) -> list[Column]: ...
    def list_indexes(schema: str, table: str) -> list[Index]: ...
    def get_table_ddl(schema: str, table: str) -> str: ...
    def kill_query(connection_id: str) -> bool: ...

class AdapterCapabilities:
    execute_select: bool
    explain: bool
    stream_rows: bool
    server_side_cancel: bool     # kill_query 是否真生效
    list_schemas: bool
    list_tables: bool
    list_columns: bool
    list_indexes: bool
    get_table_ddl: bool
```

调用方在调用前检查 `adapter.capabilities`,不支持的能力优雅降级(如 explain 不支持则前端隐藏"执行计划"按钮),不假设所有 adapter 全功能。

**实现矩阵(2.0.x)**:

| 数据库 | 实现 | 推进序 | GA 等级 | 说明 |
|---|---|---|---|---|
| MySQL | PyMySQLAdapter | 1(骨架验证) | Certified @ GA | 1.x 已有移植;测试摩擦最低,用于验证骨架 |
| DM 达梦 | DmPythonAdapter | 2 | Certified @ GA | 客户基本盘(信创/国产),1.x 已有移植 |
| Oracle | OracleDbAdapter | 3 | Certified @ 2.0.x | 客户基本盘,1.x 已有移植;允许 GA 后补齐,不卡 GA |
| PostgreSQL | PsycopgAdapter | 随元数据库 | Certified @ GA | 自身也是元数据库,天然就绪 |
| DB2 | IbmDbAdapter | 4 | Preview @ GA → Certified @ 2.1.0 | clidriver 二进制麻烦,不保证生产 |
| GaussDB / OceanBase / Hive / Trino / TDSQL | JdbcBridgeAdapter | — | 2.x 按需 | 可选扩展 |

**骨架设计约束**:首个 certified adapter 为 MySQL,但 Database Gateway 接口设计须以 **Oracle/DM 为假想验证对象**——每个 introspection / 流式 / cancel 方法都要过一遍"换成 Oracle/DM 会怎样",重点防 MySQL-specific 假设固化:标识符大小写(MySQL 不敏感 vs Oracle 默认大写)、分页语法(LIMIT vs ROWNUM/FETCH FIRST)、server-side cursor 语义、类型映射。AdapterCapabilities 矩阵(见上)是执行此约束的机制,不可形同虚设。

**等级定义**:

- **Certified**:核心能力(execute_select / explain / introspect)经测试,可用于生产
- **Preview**:能连能查,部分能力可能缺失或未充分测试,不建议生产关键路径
- **Experimental**:实验性,接口可能变

---

## 3. 技术栈选择

### 3.1 后端

| 层 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | 沿用 1.x 资产 + sqlglot/pyarrow 生态 |
| Web 框架 | FastAPI | 沿用 1.x + 依赖注入 + OpenAPI |
| 数据校验 | Pydantic v2 | 沿用 1.x |
| ASGI 运行时 | Uvicorn | hosted 多进程 / portable 单进程 |
| 元数据 ORM | SQLAlchemy 2 Core | Postgres 专用,无需多 backend 兼容 |
| 数据库迁移 | Alembic | 标准方案 |
| 主数据库 | PostgreSQL 16+ | 唯一正式 backend(全形态统一) |
| 任务队列 | PG job queue(默认) / RQ+Redis(hosted scale-out) | on-prem 不必引入 Redis |
| 进程管理(portable) | 自写轻量 launcher(Python) | 见 §6,不用 supervisord / NSSM |
| 配置管理 | pydantic-settings | 支持 env + docker secrets |
| 日志 | structlog | 结构化 JSON |
| 指标 | prometheus_client | 标准库 |
| Trace | OpenTelemetry | vendor-neutral |
| SQL 解析 | sqlglot | 沿用 1.x |
| 数据处理 | pyarrow + pandas | 沿用 1.x;ResultSet spool 用 parquet/arrow |
| 敏感数据管理 | SecretStore 抽象 + Fernet + bcrypt | 统一收口,CI lint 强制 |
| License 验签 | Ed25519(PyNaCl) | 离线公钥验签 |

### 3.2 前端

| 层 | 选择 | 理由 |
|---|---|---|
| 框架 | Vue 3 + TypeScript + Vite | 沿用 1.x 资产 |
| 路由 | Vue Router | 官方 |
| UI 状态 | Pinia | 官方推荐 |
| Server 状态 | TanStack Query(新模块) / Pinia(1.x 复用模块保留) | 不为统一强行迁旧模块,不安排专门迁移 sprint |
| SQL 编辑器 | CodeMirror 6 | 沿用 1.x |
| 图引擎 | G6 默认 + Cytoscape 实验 | 沿用 1.x |
| Virtual Scroll | vue-virtual-scroller | SQL 结果展示需要 |
| 表格 | TanStack Table | 跟 Query 配套 |
| Tailwind | Tailwind CSS v3 | 沿用 1.x |
| i18n | vue-i18n 11 | 沿用 1.x |
| 图标 | lucide-vue-next | 沿用 1.x |

### 3.3 数据库驱动

| 数据库 | Python 驱动 |
|---|---|
| MySQL | `pymysql + cryptography` |
| Postgres | `psycopg[binary] v3` |
| Oracle | `oracledb`(thin 模式) |
| DM 达梦 | `dmPython` |
| DB2 | `ibm_db` + clidriver |
| GaussDB / Hive / Trino 等 | JDBC Bridge(可选扩展,2.x 按需) |

### 3.4 部署相关

| 形态 | 容器 | 编排 | 元数据 | 队列 | 结果 | Secret |
|---|---|---|---|---|---|---|
| Hosted | Docker | 腾讯云 CVM + Compose(自管) | PG container | PG queue | 本地 volume(COS 按需) | master key file(KMS 按需) |
| On-prem Docker | Docker | Compose(api+worker+pg) | PG container | PG queue | 本地 volume | env / master key file |
| Windows Portable | 无 | launcher | Bundled PG binary | PG queue / thread | 本地目录 | master key file |

### 3.5 待定 / 待讨论

- AI Agent Loop 框架选型(2.6.0 时决策)
- 监控栈具体选择
- 腾讯云托管产品具体接入(TencentDB / COS / 云Redis,自管扛不住时再换,靠适配器)

---

## 4. 资源管理方案

### 4.1 资源 Profile

#### 4.1.1 Hosted

```yaml
hosted:
  per_tenant:
    max_jobs_per_tenant: 4
    max_jobs_per_user: 5
    sql_spool_max_rows: 1000000
    compare_batch_size: 10000
    ai_enabled: true
    ai_token_budget_per_day: 500000
```

#### 4.1.2 On-prem Docker

```yaml
onprem_docker:
  worker_replicas: 1          # 可 docker compose scale
  max_jobs_per_worker: 4
  max_jobs_per_user: 5
  sql_spool_max_rows: 1000000
  compare_batch_size: 10000
  ai_enabled: optional
```

#### 4.1.3 Portable Normal(默认,假设 8C/16G+)

```yaml
portable_normal:
  metadata_backend: bundled_postgresql
  job_backend: postgres_queue   # 或 thread,见 §6.7
  worker_processes: 1
  max_concurrent_jobs: 2
  max_jobs_per_user: 2
  sql_spool_max_rows: 500000
  compare_batch_size: 5000
  ai_enabled: false
```

#### 4.1.4 Portable Power(假设 16C/32G+)

```yaml
portable_power:
  metadata_backend: bundled_postgresql
  job_backend: postgres_queue
  worker_processes: 1
  max_concurrent_jobs: 4
  max_jobs_per_user: 4
  sql_spool_max_rows: 1000000
  compare_batch_size: 10000
  ai_enabled: optional          # 可接本地 LLM
  ai_provider_endpoint: "http://localhost:11434"
```

**不做 portable_low**:4G 机器跑 DataOpsStudio 体验差,与其降级不如不支持。**但 OOM 兜底机制必须生效**(任何 profile 下)——见 §4.2。

### 4.2 内存管理(v0.3.1 拆进程)

2.0 有多个进程(PG / API / Worker / 可选本地 AI),单一全局值无法判断超在哪。拆分:

```yaml
memory_guard:
  mode: enforce
  check_interval_seconds: 10

  process:
    api_soft_mb: 1024            # API 进程预期上限(只接请求,不跑大任务)
    api_hard_mb: 2048
    worker_soft_mb: 4096         # worker 跑大任务,主要内存消耗者
    worker_hard_mb: 6144
    postgres_expected_mb: 512    # 内嵌 PG 预期(portable),仅作系统预留参考

  per_job:
    sql_query_limit_mb: 1024     # 单 sql_query job 内存上限
    compare_limit_mb: 2048
    export_limit_mb: 1024

  system:
    min_free_mb: 2048            # 系统剩余低于此值,拒绝新 job

  resultset:
    auto_evict: true             # 全局兜底:超 spool 上限自动 evict
    spool_max_bytes: 536870912
```

**OOM 兜底**(任何 profile / 任何配置生效):单 job 超 `per_job` 上限 → 终止该 job 并标记;系统剩余低于 `min_free_mb` → 拒绝新 job 入队。

### 4.3 磁盘管理

```yaml
disk_guard:
  enforce: true
  min_free_gb: 5
  max_disk_usage_percent: 85
  mid_run_check_interval_rows: 5000
  default_run_quota_mb: 5120
  max_run_quota_mb: 51200
  default_project_quota_gb: 50
  result_ttl_days: 90
  resultset_spool_ttl_days: 7    # SQL Workspace spool 较短
  sql_export_ttl_hours: 24
  ai_session_ttl_days: 30
  tmp_ttl_hours: 6
```

### 4.4 连接池管理

```yaml
db_connection_pool:
  per_datasource:
    max_size: 4
    idle_seconds: 600
    pre_ping: true
    timeout_seconds: 30
  # 注:不再有 long_running_pool 绑 ResultSet。
  # ResultSet 改 spool 后,连接只在 worker 拉取期间持有,拉完即还。

metadata_pool:
  api:
    pool_size: 10
    max_overflow: 20
  worker:
    pool_size: 5                 # worker 单独的元数据连接池
  pool_recycle: 1800
  pool_pre_ping: true
```

### 4.5 并发与限流

```yaml
job_concurrency:
  max_jobs_total: <profile>
  max_compare_jobs: 2
  max_export_jobs: 1
  max_sql_explain_jobs: 5
  max_ai_assist_jobs: 10
  max_ai_copilot_jobs: 5
  max_jobs_per_user: <profile>
  max_sql_query_per_user_per_datasource: 3

rate_limit:
  enforce: true
  login_per_ip_per_min: 10
  api_per_user_per_min: 60
  ai_calls_per_user_per_day: 500
  export_per_user_per_hour: 10
```

### 4.6 超时管理

```yaml
timeouts:
  http_request_default: 30
  http_request_long: 300
  db_statement_default: 900
  db_statement_preview: 60
  db_statement_compare: 1800
  cursor_max_hold_seconds: 300   # DB cursor 最长持有(配合 spool)
  job_default_timeout: 1800
  sql_query_timeout: 900
  compare_run_timeout: 3600
  scenario_run_all_timeout: 1800
  ai_assist_timeout: 60
  ai_copilot_timeout: 300
  worker_heartbeat_interval: 15
  worker_heartbeat_timeout: 600  # 超时视为 worker 失联;慢首行 OLAP 优先避免误杀
  job_history_retention_days: 30
  download_url_ttl_seconds: 300
  session_absolute_timeout_hours: 8
```

---

## 5. 数据存储与迁移

### 5.1 元数据 Schema 策略

**唯一存储**:PostgreSQL 16+。

**核心表**(敏感字段全部走 secret_ref,见 §7 分级):

```sql
-- 用户与项目
users (id, username, password_hash, mfa_secret_ref VARCHAR(64), role, ...)
projects (id, name, owner_user_id, ...)
project_members (project_id, user_id, role)

-- 数据源
datasources (
  id, project_id, name, db_type, host, port, username,
  database_name VARCHAR(128) NULL,            -- 业务连接的库/schema/service;
                                              -- 映射 DatasourceConnInfo.database
                                              -- ★ 列名用 database_name(database 是 SQL 保留字)
                                              -- ★ NULL + 应用层 Pydantic 非空校验,避 DEFAULT '' garbage
                                              -- 2.0.0 alembic migration 0002 引入
  password_secret_ref VARCHAR(64) NOT NULL,   -- Application Secret
  environment, capability_profile_jsonb, ...
)

-- AI 配置
ai_configs (id, provider, api_key_secret_ref VARCHAR(64), egress_policy_jsonb, ...)

-- SQL Workspace
sql_consoles (id, owner_user_id, name, datasource_id, sql, ...)
sql_executions (id, console_id, sql, sql_hash, status, result_set_id, ...)
result_sets (id, execution_id, storage_ref, columns_jsonb, loaded_rows,
             total_rows, state, created_at)   -- 不存 cursor
sql_templates (id, name, sql, tags, db_types, ...)

-- 任务
compare_tasks (id, project_id, name, source_id, target_id, ...)
workflows (id, project_id, name, dag_jsonb, schedule_cron, ...)

-- Job 统一表(含 queue 字段)
jobs (id, kind, status, owner_user_id, project_id, priority,
      worker_id, last_heartbeat, payload_jsonb, result_ref,
      cancel_requested, retry_count, ...)
job_events (job_id, ts, event_type, message, ...)

-- 结果索引
run_index (run_id, kind, owner_user_id, project_id, result_path, ...)

-- 审计
audit_logs (id, ts, user_id, action, resource_type, resource_id, request_id, ...)

-- SecretStore(仅 Application Secret;Bootstrap Secret 不入库,见 §7)
secret_refs (
  ref VARCHAR(64) PRIMARY KEY, kind VARCHAR(32) NOT NULL,
  ciphertext BYTEA NOT NULL, created_by, created_at, last_accessed_at,
  rotation_required BOOL DEFAULT false
)

-- AI
ai_sessions (id, owner_user_id, project_id, context_jsonb, ...)
ai_memory (session_id, vector, content, metadata_jsonb)   -- pgvector
ai_usage (ts, user_id, kind, provider, purpose, egress_level,
          tokens_in, tokens_out, cost, ...)
ai_user_preferences (user_id, project_id, kind, value_jsonb, ...)

-- Scenario / Lineage / Asset
scenario_templates (id, project_id, yml_text, ...)
asset_aspects (asset_type, asset_id, aspect_type, value_jsonb, ...)  -- 核心6+旧6可读
asset_aspect_history (...)
lineage_runs (id, project_id, datasource_id, sql_hash, dialect, status,
              source_ref, parse_summary_jsonb, created_at, ...)
lineage_edges (id, run_id, project_id, source_table, target_table,
               edge_kind, depth_hint, inferred BOOL, inference_status,
               confidence, source_statement_index, sql_hash, metadata_jsonb, ...)
lineage_column_edges (id, run_id, source_table, source_column,
                      target_table, target_column, transformation,
                      transformation_subtype, inferred BOOL,
                      inference_status, confidence, sql_hash, metadata_jsonb, ...)

-- License(portable / on-prem)
license_state (id, edition, customer, expires_at, features_jsonb,
               signature_verified_at, grace_started_at, mode, ...)
```

### 5.2 多租户隔离(hosted,2.7.0+)

- **MVP(2.7.0)**:app 层 project_id 隔离 + per-tenant quota
- **Beta(2.7.x)**:Postgres RLS,object storage prefix 隔离,audit 分区
- **Enterprise**:单租户 dedicated deployment
- **on-prem / portable**:单租户,不启用 RLS

### 5.3 结果存储抽象

```python
class ResultStore(Protocol):
    def put_artifact(run_id: str, name: str, stream: BinaryIO) -> ResultRef: ...
    def append_spool(result_set_id: str, rows: list[Row]) -> None: ...  # 流式追加
    def get_manifest(run_id: str) -> Manifest: ...
    def fetch_range(result_set_id: str, offset: int, limit: int) -> list[Row]: ...
    def open_download(ref: ResultRef) -> BinaryIO: ...
    def delete_run(run_id: str) -> None: ...
    def gc_expired() -> int: ...

class LocalFsResultStore: ...
class S3ResultStore: ...
```

ResultSet spool 用 parquet / arrow 落地,支持 offset/limit 范围读。

### 5.4 1.x → 2.0 迁移

#### 5.4.1 migrate_from_v1.py

第一个 PR 必须提供,持续维护到 2.0 stable。**迁移目标:统一 PostgreSQL**。

| 源(1.x) | 目标(2.0 PG) | 敏感数据处理 |
|---|---|---|
| `config/users.json` | `users` | password_hash 直接迁;`mfa_secret_encrypted`(Fernet)用新 master key 重加密;recovery codes(bcrypt)直接迁 |
| `config/projects.json` | `projects` + `project_members` | `Project.members`(User.id 数组)拆成 project_members 关联表 |
| `config/datasources.json` | `datasources` | 明文 `password` 字段 → SecretStore → secret_ref;`extra` dict 转 capability_profile |
| `config/tasks.json` | `compare_tasks` | — |
| `config/workflows.json` | `workflows` | **节点白名单校验,含禁止节点则报错** |
| `config/workflow_templates.json` | `workflow_templates` | (v0.3.2 补:此前漏列) |
| `config/scenarios/*.yml` | `scenario_templates` | — |
| `config/sql_templates.json` | `sql_templates` | — |
| `config/lineage_ai.json` | `ai_configs` | `api_key_encrypted`(Fernet)用新 master key 重加密 |
| `config/asset_aspects.yml` | aspect 类型定义迁入 | 6 个资产分类类型定义(非数据) |
| `config/.dataops_secret.key` | **迁移输入(不是迁移目标)** | 旧 Fernet key,用于解密上述加密字段,**迁移工具的必需输入** |
| `config/metadata_cache/*.json` | **不迁** | sqlide 元数据缓存,2.0 重建即可 |

**1.x SQLite(`data/dataops.db`)9 张表迁移**(AS-IS §3.2):

| 1.x SQLite 表 | 2.0 PG 目标 | 说明 |
|---|---|---|
| `audit_logs` | `audit_logs` | 直接迁(**不要再读 logs/audit.jsonl,1.x 启动已 migrate 进 SQLite**) |
| `jobs` | `jobs` | 直接迁(**不要再读 config/jobs.json,已迁进 SQLite**);running 状态迁入时标 failed |
| `asset_aspects` | `asset_aspects` | 资产分类数据,直接迁 |
| `asset_aspect_history` | `asset_aspect_history` | append-only 历史,直接迁 |
| `refresh_tokens` | `refresh_tokens` | rotation 链 |
| `revoked_tokens` | `revoked_tokens` | JWT 吊销 |
| `download_nonces` | (可选迁/可丢) | 一次性 nonce,丢弃影响小 |
| `slow_sql_plans` | `slow_sql_plans` | plan-diff 历史 |
| `run_index` | `run_index` | compare run 统一索引 |

**结果文件 / lineage**:

| 源 | 处理 |
|---|---|
| `results/<run_id>/`(parquet 目录)+ `.xlsx` + `workflow_runs/` + `sql_exports/` | 保留原路径 + 写 `run_index` 反向索引 |
| **lineage 解析结果** | **不迁**(1.x 本就不持久化,每次重算,见 §2.4);2.0 内部产生的 `lineage_edges` / `lineage_column_edges` 由 2.4.0 起持久化,与 1.x 迁移无关 |

**关键修正说明(v0.3.2,基于 AS-IS 核实)**:
- audit_logs / jobs 只从 SQLite 迁,**不重复读 jsonl / json**(1.x 启动已迁,再读会重复)
- 补迁 workflow_templates(此前漏)
- SQLite 是 **9 张明确的表**,逐表映射,不是笼统"表对表"
- `.dataops_secret.key` 是**迁移输入**(解密旧数据用),不是迁移目标
- 1.x lineage 解析结果不迁(无 aspect 收敛,无历史 aspect 数据可迁);2.0 的 lineage 边表持久化是 ADR-0020 新决策,不要求迁移工具从 1.x 构造历史边表

**Workflow 节点白名单校验**:迁移时发现 1.x workflow 含 2.0 禁止节点(shell / 外部 HTTP 等),迁移工具报错并要求用户手动改写,不静默丢弃。

#### 5.4.2 迁移流程

```
1. 1.x 实例停机
2. 备份 1.x 所有数据(config + results + data + logs + .dataops_secret.key)
3. 起空 2.0 实例(任一形态,PG 已就绪)
4. 生成新 master key
5. python migrate_from_v1.py --source <v1> --target <2.0> --v1-secret-key <key>
6. 校验:
   - 导入条数与 1.x 一致
   - 抽样 datasource test_connection 成功(密码解密正确)
   - 抽样 AI provider ping 成功(API key 重加密正确)
   - 历史 run 能查看
   - Workflow 全部能加载(无禁止节点残留)
7. 2.0 实例接管
8. 1.x 保留 30 天回滚兜底
9. 30 天后物理销毁 1.x datasources.json
```

**关键约束**:迁移前强制备份;迁移日志不含明文密码;迁移失败保留已写入部分但 2.0 不启动(防半成品被认为成功)。

#### 5.4.3 不向后兼容承诺

API 路径 / 数据 schema(SQLite→PG)/ 配置格式 / 敏感数据存储方式 / Workflow 节点定义均变化。客户必须按官方迁移路径走,不支持就地升级。

---

## 6. Portable Runtime Supervisor(v0.3.1 重写)

### 6.1 设计目标与定位

Windows Portable 用户无需安装 PostgreSQL、无需了解 PG 运维,解压即用。

**定位(明确边界)**:

- 比 `start.ps1` 直接拉 API **更稳**(API 崩了不残留 PG 进程 / 端口 / pid)
- 比 Windows Service / 自动重启 supervisor **更轻**(不做崩溃自愈、健康探针、服务注册)
- 足够支撑 2.0.0 portable,不把单人开发拖进过度运维工程

**launcher 不是高可用组件**:它管生命周期(启动顺序、优雅停止、异常清理),不管高可用(自动重启、故障转移)。worker / API 崩溃时,launcher 记录日志并停止整体,由用户重新 start。

### 6.2 进程模型

```
start.ps1
   └─> python -m app.launcher       (单一启动入口,轻量 Python 进程)
          │
          ├─ 1. 检查/启动 PostgreSQL(子进程)
          ├─ 2. 等待 PG ready
          ├─ 3. 执行 Alembic migration
          ├─ 4. 启动 API(子进程)
          ├─ 5. 启动 Worker(子进程)
          ├─ 6. 写 pid file,监听子进程状态
          └─ 7. 收到退出信号 → 反序优雅停止所有子进程
```

launcher 是个轻量进程(目标 200-300 行),职责清单:

- 端口探测(PG / API)
- 按顺序启动 PG → migrate → API → worker
- 维护 `data/launcher.pid` 与各子进程 pid
- 转发 Ctrl-C / 关闭信号,反序优雅停止
- 任一关键子进程异常退出 → 记录日志 + 优雅停止其余进程(不自动重启)
- 退出时清理 pid file、确认 PG 已 stop、释放端口

### 6.3 Managed Local PostgreSQL 启动流程

```
launcher 启动 PG 阶段:
1. 检查 data/pgdata 是否存在
   ├─ 不存在 → initdb
   │   ├─ 生成 PG superuser 密码(随机 32 字节)
   │   ├─ 生成 PG app user(dataops)+ 密码(随机 32 字节)
   │   ├─ 密码写入 config/secrets/(Bootstrap Secret,见 §7,权限 0600)
   │   └─ 写 postgresql.conf + pg_hba.conf
   └─ 存在 → 跳过 initdb
2. 探测端口:默认 127.0.0.1:15432,占用则递增 15433~15499
   实际端口写入 config/runtime.json
3. pg_ctl start(仅监听 127.0.0.1)
4. 等待 PG ready(最长 30s),失败则 fail-fast,不继续启动 API
```

### 6.4 PostgreSQL 配置默认值

**postgresql.conf**:

```conf
listen_addresses = '127.0.0.1'
port = 15432
max_connections = 30
shared_buffers = 256MB
work_mem = 8MB
maintenance_work_mem = 64MB
effective_cache_size = 1GB
wal_level = replica
logging_collector = on
log_directory = '../logs/pg'
log_min_duration_statement = 1000
```

**pg_hba.conf**(仅 loopback,scram-sha-256):

```conf
local   all   all                  scram-sha-256
host    all   all   127.0.0.1/32   scram-sha-256
host    all   all   ::1/128        scram-sha-256
```

不监听 `0.0.0.0`,不接受外部连接。

### 6.5 脚本

| 脚本 | 职责 |
|---|---|
| `start.ps1` | 启动 launcher |
| `stop.ps1` | 通知 launcher 优雅停止(SIGTERM → 等 active job → 反序停子进程) |
| `backup.ps1` | `pg_dump --format=custom` 到 `backups/dataops_<ts>.dump` |
| `restore.ps1` | 需先 stop,`pg_restore --clean --if-exists` |
| `upgrade.ps1` | 见 §6.6 |

### 6.6 升级策略

```
upgrade.ps1:
1. stop.ps1
2. backup.ps1(强制备份)
3. 区分 PG 版本:
   ├─ 小版本(16.x→16.y):替换 runtime/postgres binary,保留 pgdata
   └─ 大版本(16→17):pgdata→pgdata.old → initdb 新 → pg_restore from backup → 验证后删 old
4. 替换 app/ 目录
5. start.ps1(launcher 自动 Alembic migration)
```

**升级永不覆盖**:`config/.secret_master.key` / `config/secrets/*` / `config/runtime.json` / `config/license.lic`。覆盖了加密数据无法解密、license 失效。

### 6.7 简化模式:thread backend

极简部署(单用户、不需要进程隔离)可用 thread backend——launcher 只启 PG + 单进程(API + worker 同进程 ThreadPool)。配置 `job_backend: thread`。适用于:个人 dogfooding、资源极有限的环境。**这是 portable 的可选简化,不是 on-prem 的默认**(on-prem 默认 API/worker 分离)。

### 6.8 目录布局(portable)

```
DataOpsStudio-portable/
├── app/                       # Python 业务代码
├── runtime/
│   ├── python/                # 内嵌 Python
│   └── postgres/bin|lib/      # 内嵌 PG binary
├── config/
│   ├── dataops.yml
│   ├── runtime.json           # 自动生成:实际 PG/API 端口
│   ├── .secret_master.key     # Bootstrap: master key,0600
│   ├── license.lic            # Bootstrap: license
│   └── secrets/               # Bootstrap secrets,0600
│       ├── pg_app_password
│       └── pg_superuser_password
├── data/
│   ├── pgdata/                # PG 数据目录
│   ├── results/               # Job 结果 + ResultSet spool
│   ├── ai_memory/             # pgvector(如启用)
│   └── launcher.pid
├── logs/{app,worker,pg}/
├── tmp/
├── backups/                   # pg_dump 输出
└── scripts/{start,stop,backup,restore,upgrade}.ps1
```

### 6.9 用户文档要求(`docs/portable-quickstart.md`)

1. 杀毒提示:建议将 `runtime/postgres/bin/postgres.exe` 与 `data/pgdata/` 加入信任目录(不强制)
2. Windows 防火墙弹窗可拒绝(PG 只监听 loopback)
3. 数据备份责任:用户定期 `backup.ps1`,2.0.0 不做自动备份调度
4. 不要用外部工具(pgAdmin 等)直连 pgdata,所有操作走脚本
5. 实际端口见 `config/runtime.json`

### 6.10 失败模式与排查

| 症状 | 原因 | 排查 |
|---|---|---|
| launcher 报 "could not bind socket" | 端口 15432-15499 全占 | 关其他 PG / 改起始端口 |
| pgdata 损坏 | 强制关机 / 杀毒隔离 | restore.ps1 从备份恢复 |
| worker 启动失败 | 依赖缺失 / 配置错 | 看 logs/worker/,修后 start |
| Alembic 失败 | 大版本未走 dump/restore | 走 upgrade.ps1 |
| PG 启动慢 | 杀毒实时扫描 | 加信任目录 |

### 6.11 不做(2.0.0)

- ❌ 自动备份调度(用户手动 backup.ps1)
- ❌ Windows Service 注册(2.x 按需)
- ❌ 崩溃自动重启 / 健康探针(launcher 只记录 + 优雅停止)
- ❌ PG 内部健康监控 UI
- ❌ 跨机器迁移(走 backup → restore)

---

## 7. 安全与敏感数据管理(v0.3.1 修正:Bootstrap vs Application Secret)

### 7.1 1.x 现状审计

1.x 6 种敏感数据,处理方式不统一:

| 数据 | 1.x 处理 | 状态 |
|---|---|---|
| 用户登录密码 | bcrypt hash 落 users.json | ✅ |
| JWT secret | env var | ✅ |
| Refresh token | SQLite | ✅ |
| AI API key | Fernet 加密落 lineage_ai.json | ✅ |
| MFA TOTP seed | Fernet 加密落 users.json | ✅ |
| Datasource password | **明文落 datasources.json** | ❌ 漏洞 |

根本问题:5 种各自实现,1 种漏了——没有统一抽象的必然结果。

### 7.2 1.x v1-lts 立刻 Hotfix(P0,1-2 天)

1. 扩展 `secret_crypto.py` 覆盖 datasource password
2. 启动时自动迁移明文 → 加密
3. 业务读密码改 `get_datasource_password(ds)` helper
4. gitleaks 规则:`config/*.json` 出现明文 `"password":` CI fail
5. SECURITY.md 说明 + 建议用户改密
6. 发版 `1.x.y+1`

### 7.3 Secret 两层模型(v0.3.1 核心修正)

**问题**:若 PG 连接密码也存在 PG 的 `secret_refs` 表里,会产生启动循环——连 PG 需要密码,密码却在 PG 里。

**解法**:Secret 分两类。

#### 7.3.1 Bootstrap Secret(连 PG 之前必须可用)

存于**文件系统**,由 Portable Runtime / 部署环境管理,**不入 PG**:

| Bootstrap Secret | 位置 | 权限 |
|---|---|---|
| master key | `config/.secret_master.key` | 0600 |
| PG app user password | `config/secrets/pg_app_password` | 0600 |
| PG superuser password | `config/secrets/pg_superuser_password` | 0600 |
| license file | `config/license.lic` | 0600 |

形态差异:

- **portable**:launcher 生成并写入文件(initdb 时)
- **on-prem Docker**:docker secret / env 注入,或挂载加密文件
- **hosted**:PG 连接凭据由 KMS / Secret Manager / IAM 管理,不落普通文件

#### 7.3.2 Application Secret(系统起来后管理)

存于 PG `secret_refs` 表,用 master key(Fernet)加密:

| Application Secret | SecretKind |
|---|---|
| datasource password | DATASOURCE_PASSWORD |
| AI API key | AI_API_KEY |
| MFA TOTP seed | MFA_TOTP_SEED |
| OAuth token | OAUTH_TOKEN |
| webhook secret | WEBHOOK_SECRET |
| signed URL secret | SIGNED_URL_SECRET |

**关键**:master key 是 Bootstrap Secret(文件系统),用它解密 PG 里的 Application Secret。启动顺序:读 master key(文件)→ 读 PG 密码(文件)→ 连 PG → 用 master key 解密 PG 里的 Application Secret。无循环。

### 7.4 SecretStore 抽象(仅管 Application Secret)

```python
class SecretStore(Protocol):
    def store_secret(plaintext: str, kind: SecretKind) -> SecretRef: ...
    def reveal_secret(ref: SecretRef) -> str: ...
    def rotate_secret(ref: SecretRef, new_plaintext: str) -> SecretRef: ...
    def delete_secret(ref: SecretRef) -> None: ...
    def hash_password(plaintext: str) -> HashedRef: ...
    def verify_password(plaintext: str, ref: HashedRef) -> bool: ...
    def rotate_master_key(new_key: bytes) -> RotationReport: ...

class BootstrapSecrets:
    """Bootstrap Secret 读取,不走 PG,只读文件系统 / env / KMS"""
    def get_master_key() -> bytes: ...
    def get_pg_app_password() -> str: ...
    def get_pg_superuser_password() -> str: ...
    def get_license_file() -> Optional[bytes]: ...
```

| 形态 | SecretStore 实现(Application) | Bootstrap 来源 |
|---|---|---|
| Hosted | KmsSecretStore | KMS / Secret Manager |
| On-prem Docker | Env/FileSecretStore | docker secret / env / file |
| Windows Portable | LocalFileSecretStore | `config/.secret_master.key` + `config/secrets/` |

### 7.5 业务代码规范与 CI Lint

- 业务代码不直接持有明文密码超过一个调用栈
- 持久化对象只存 SecretRef
- DB 连接借密码必经 Database Gateway(内部调 reveal_secret)
- SecretStore / BootstrapSecrets 之外不允许 import Fernet / bcrypt 等原语

CI 检查:gitleaks + config 明文字段检测 + 业务模块直接读 `ds["password"]` 检测 + crypto import 越界检测。

### 7.6 master key 管理与轮换

- portable / on-prem:首次启动生成 32 字节随机 key,写 0600 文件;客户**必须手动备份**(不备份 = 数据无法恢复,文档强调)
- hosted:KMS 自动管理
- 轮换:`rotate-master-key` admin 命令——旧 key 解密所有 Application Secret → 新 key 重加密 → 写回。Bootstrap Secret(PG 密码等)单独轮换。

### 7.7 审计

每次 `reveal_secret` 落审计(ts / action / user_id / secret_ref / kind / purpose / request_id)。异常检测:单用户 1 分钟 reveal 同一 ref 超 10 次 → 告警。

---

## 8. License 模型(v0.3.1 修正:Repair Mode)

### 8.1 适用范围

适用于 portable / on-prem Docker。Hosted 不用 license file(走账号体系)。

**注**:本节定义 license 的**技术机制**;商业策略(定价 / 套餐)按 §1.5 不在 2.0 阶段做。

### 8.2 设计原则

- 离线验签(本地公钥,不联网)
- 不硬件绑定
- 不重反破解(合规提醒,非技术防破解)
- **失败不锁死系统**(v0.3.1 修正):invalid / expired 进入 Repair Mode,而非拒绝启动
- 公钥内置二进制,license 用 Ed25519 签名

### 8.3 License File 格式(`config/license.lic`)

```json
{
  "payload": {
    "schema_version": "1.0",
    "license_id": "DOS-2026-XXXX",
    "customer": "<CUSTOMER>",
    "edition": "portable|onprem|enterprise",
    "issued_at": "2026-01-01T00:00:00Z",
    "expires_at": "2027-01-01T00:00:00Z",
    "features": ["sql_workspace","compare","scenario_lab","lineage","workflow","ai_gateway","ai_assist","ai_copilot"],
    "limits": {"max_users": 5, "max_datasources": 50, "max_projects": 10},
    "offline": true
  },
  "signature": "<base64 Ed25519 signature>"
}
```

### 8.4 License 状态与影响(v0.3.1 修正)

| 状态 | 影响 |
|---|---|
| trial(无 license) | 30 天试用,功能不限,UI 倒计时 |
| valid | 全功能 |
| in_grace(过期 ≤7 天) | 只读 + 允许 license 更新 + 允许备份;禁建任务 / 禁 AI |
| expired(过期 >7 天) | 进入 Repair Mode |
| invalid_signature | 进入 Repair Mode(**不再拒绝启动**) |

### 8.5 Repair Mode(v0.3.1 新增)

license 损坏 / 过期 / 签名错误时,系统仍能启动进入 Repair Mode。

**允许**:上传/替换 license、查看机器/诊断信息、导出诊断包、备份数据、恢复备份、只读查看审计与配置。

**禁止**:新增/修改数据源、执行 SQL、运行任务、AI 调用、导出业务结果。

**理由**:离线金融客户现场,license 文件传输出错(BOM / 换行 / 截断)是常见情况。直接拒绝启动会让客户连"上传新 license"都做不到。Repair Mode 保证客户能自救。

### 8.6 验签流程

```python
def verify_license() -> LicenseState:
    path = Path("config/license.lic")
    if not path.exists():
        return LicenseState.trial_30day()
    try:
        data = json.loads(path.read_text())
        payload = canonical_json(data["payload"]).encode()
        signature = base64.b64decode(data["signature"])
        PUBLIC_KEY.verify(signature, payload)   # Ed25519
    except (JSONDecodeError, KeyError, InvalidSignature, ValueError):
        return LicenseState.repair_mode("invalid_signature")
    expires = parse_iso(data["payload"]["expires_at"])
    now = datetime.now(timezone.utc)
    if now > expires:
        if now < expires + timedelta(days=7):
            return LicenseState.in_grace(data["payload"], expires)
        return LicenseState.repair_mode("expired")
    return LicenseState.valid(data["payload"])
```

### 8.7 特性开关

`features` 列表控制可用能力域。未授权:后端 403 + `feature_not_licensed`,前端灰显 + tooltip。

### 8.8 公钥管理

Release pipeline 用私钥签 license(私钥永不离开签发机);二进制内置公钥(每 release 内置);不做 license server(2.0.0)。

### 8.9 不做(2.0.0)

联网激活 / 续期、硬件指纹绑定、license server / 在线撤销、复杂反破解、per-feature 灰度激活。

---

## 9. 日志与可观测性(v0.3.2 新增)

> 注:原 §9 商业模式骨架已在 v0.3.1 删除(2.0 阶段不做商业模式;license 技术机制见 §8)。本章节重新启用 §9 编号,内容为日志与可观测性设计。

### 9.1 三条管线(必须分离)

"日志"是三种不同目标的东西,留存与脱敏要求互相冲突,因此**分三条独立管线**,各走各的存储与策略,不混为一坨:

| 管线 | 目标 | 存储 | 留存 | 脱敏 | 对应零件 |
|---|---|---|---|---|---|
| **运行日志(Runtime Log)** | A 排障 | 文件 / stdout | 短期(滚动) | **强制脱敏** | structlog |
| **审计日志(Audit Log)** | B 合规 | PG `audit_logs` 表 | 长期 | 结构化字段,不存敏感值 | audit_logs 表 |
| **指标(Metrics)** | C 可观测 | Prometheus | 聚合数值 | 无敏感信息(只数值) | prometheus_client |

**核心矛盾与解法**:排障要详细(越详细越好定位),合规要脱敏(绝不能见敏感数据)——两者直接对撞。解法不是折中,而是分管线:运行日志强制脱敏后仍可详细(记结构、记 hash、记长度,不记值);需要"谁动了什么"的可追溯性由审计日志单独承担。

### 9.2 运行日志(A 排障)

#### 9.2.1 结构化 JSON + 关联 ID

全部 structlog 输出 JSON,每条日志强制带关联字段,以便把"一次请求"或"一个任务"的所有日志串起来:

```json
{
  "ts": "2026-05-28T10:00:00.123Z",
  "level": "info",
  "logger": "worker.sql_executor",
  "event": "query_executed",
  "request_id": "req_abc123",
  "job_id": "job_xyz789",
  "user_id": "u_001",
  "project_id": "p_005",
  "datasource_id": "ds_042",
  "duration_ms": 1234,
  "rows_loaded": 5000,
  "sql_hash": "sha256:9f8e...",
  "process": "worker"
}
```

**关联 ID 贯穿**:`request_id` 在 API 入口生成(§2.3 RequestId middleware),随 Job 一起传到 worker,worker 日志带同一个 `request_id` + `job_id`。金融客户内网排障时,让客户导出"带某 request_id 的所有日志"发给你,即可远程定位——这是单人 + 客户内网过不去场景下的核心排障手段。

#### 9.2.2 强制脱敏(与 §7 安全设计强绑定)

**这是运行日志最关键的一条,也是 SecretStore 的隐藏漏洞口**:辛苦做了 SecretStore 不落明文密码,若排障时一行 `log.info("connect", password=pwd)` 把密码打进日志,前功尽弃。

脱敏不能靠开发者自觉,必须做成 **structlog 全局 processor**,在日志写出前强制拦截:

```python
SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "api_key",
                  "mfa_seed", "authorization", "cookie", "ciphertext"}

def redact_processor(logger, method_name, event_dict):
    # 1. 按 key 名拦截:敏感字段直接替换
    for k in list(event_dict.keys()):
        if any(s in k.lower() for s in SENSITIVE_KEYS):
            event_dict[k] = "***REDACTED***"
    # 2. SQL 不记原文,只记 hash + 长度(原文 L3,见 §2.7.5)
    if "sql" in event_dict:
        sql = event_dict.pop("sql")
        event_dict["sql_hash"] = sha256(sql)
        event_dict["sql_len"] = len(sql)
    # 3. 样本数据值(L4/L5)永不进日志
    if "rows" in event_dict or "sample" in event_dict:
        event_dict.pop("rows", None)
        event_dict.pop("sample", None)
        event_dict["rows_omitted"] = True
    # 4. 正则兜底:身份证 / 手机号等模式命中即打码
    return apply_pattern_redaction(event_dict)
```

**原则**:日志记**结构、hash、长度、计数**,不记**值**。要查"哪条 SQL",用 `sql_hash` 关联到 `sql_executions` 表(那里有原文,受 AuthZ 保护),而不是把 SQL 打进日志文件。

#### 9.2.3 日志级别

| 级别 | 用途 | 默认开 |
|---|---|---|
| DEBUG | 详细调用链,开发/深度排障 | 否(按需开) |
| INFO | 正常业务事件(请求/任务起止) | 是 |
| WARNING | 可恢复异常(重试、降级、超限) | 是 |
| ERROR | 失败需关注(任务失败、连接失败) | 是 |
| CRITICAL | 系统级故障(PG 不可用、启动失败) | 是 |

DEBUG 默认关,通过配置 / 环境变量 / 运行时 admin 开关临时开启(便于客户现场临时升级日志级别配合排障,排完关掉)。

#### 9.2.4 三形态落地

| 形态 | 输出 | 收集 |
|---|---|---|
| Portable | 文件 `logs/{app,worker,pg}/` + 滚动 | 用户手动导出发你 / 诊断包 |
| On-prem Docker | stdout(JSON) | docker logs / 客户日志系统(ELK 等) + 可选文件 |
| Hosted | stdout(JSON) | 腾讯云 CLS 或自管 Loki |

**Portable 诊断包**:配合 §8 License Repair Mode,提供"导出诊断包"功能——打包近期脱敏运行日志 + 系统信息 + 配置(脱敏)成一个 zip,客户一键导出发你。这是你过不去内网时的救命功能。

#### 9.2.5 滚动与留存

```yaml
runtime_log:
  format: json
  level: info
  rotation:
    max_size_mb: 100
    max_files: 10            # 单类型最多保留 10 个滚动文件
  retention_days: 30         # 配合 §4.3 disk_guard
  per_process: true          # app / worker / pg 分目录
```

worker 日志是排障重点(长任务 / DB 访问 / AI 调用都在 worker),滚动文件数可单独调大。

### 9.3 审计日志(B 合规)

#### 9.3.1 定位

审计日志回答"**谁,在什么时候,对什么资源,做了什么,结果如何**"。进 PG `audit_logs` 表(§5.1 已有),不进文件——因为要查询、要关联用户/项目、要长期留存、金融客户安全团队要审计与导出。

```sql
audit_logs (
  id, ts, user_id, project_id,
  action,              -- login / sql_execute / datasource_create / secret_reveal / export / ...
  resource_type,       -- datasource / sql_console / compare_task / secret / ...
  resource_id,
  result,              -- success / denied / error
  request_id,          -- 关联运行日志
  ip, user_agent,
  detail_jsonb         -- 操作摘要,同样不存敏感值
)
```

#### 9.3.2 必审计事件

至少覆盖(金融客户合规基线):

- 认证:登录 / 登出 / 失败登录 / MFA
- 授权:权限拒绝(AuthZ deny)
- 数据源:增删改 / 测试连接
- SQL:执行(记 sql_hash 不记原文)/ 导出
- Secret:**每次 reveal_secret**(§7.7 已要求)
- AI:每次出站调用(记 purpose / egress_level,不记内容)
- 配置:权限变更 / 用户管理
- License:状态变更 / Repair Mode 进入

#### 9.3.3 与运行日志的关系

审计日志和运行日志通过 `request_id` 关联:审计回答"做了什么"(合规视角,长期留存),运行日志回答"怎么做的、哪里出错"(排障视角,短期)。同一次操作两条线都有记录,但目标不同、留存不同、存储不同。

#### 9.3.4 不可篡改倾向

2.0.0 不做完整的防篡改(WORM / 区块链式哈希链),但做到:

- 审计写入与业务操作在同一事务(操作成功才审计,审计失败回滚)
- audit_logs 表对普通用户只读(应用层),只有系统写入
- 留存期远长于运行日志(按客户合规要求,默认 ≥ 1 年,可配)

完整防篡改(哈希链 / 外部归档)后置,非 2.0.0。

### 9.4 指标(C 可观测)

#### 9.4.1 定位

Prometheus 暴露聚合数值,回答"系统健不健康",不含任何敏感信息(只有数值和低基数标签)。

#### 9.4.2 核心指标

```
# 任务
dataops_jobs_total{kind, status}                    counter
dataops_job_duration_seconds{kind}                  histogram
dataops_jobs_active{kind}                           gauge
dataops_job_queue_depth                             gauge      # PG queue 待处理数

# SQL / DB
dataops_sql_query_duration_seconds{datasource_type} histogram
dataops_db_connection_pool_in_use{datasource_id}    gauge
dataops_slow_query_total{datasource_type}           counter

# 资源(配合 §4.2 拆进程)
dataops_process_memory_mb{process}                  gauge      # api/worker/pg
dataops_disk_free_gb                                gauge
dataops_resultset_spool_bytes                       gauge

# AI
dataops_ai_calls_total{purpose, provider, egress_level}  counter
dataops_ai_tokens_total{purpose, direction}         counter
dataops_ai_call_duration_seconds{purpose}           histogram

# Worker 健康
dataops_worker_heartbeat_age_seconds{worker_id}     gauge
dataops_worker_up{worker_id}                         gauge
```

#### 9.4.3 三形态

| 形态 | 指标 |
|---|---|
| Portable | `/metrics` 端点暴露(本机可看),不强制接监控;轻量 |
| On-prem Docker | `/metrics`,客户可接自己的 Prometheus/Grafana |
| Hosted | `/metrics` → 腾讯云 Prometheus 监控 或自管 Grafana |

#### 9.4.4 告警(可观测的落地)

2.0.0 不内置告警引擎,只暴露指标。告警靠外部(客户的 Prometheus Alertmanager / 你 hosted 的 Grafana)。但**应用内置几个关键阈值的日志告警**(写 ERROR 级运行日志):磁盘低于 min_free_gb、worker 心跳超时、PG 不可达、AI budget 超限。

### 9.5 OpenTelemetry Trace(2.x,非 2.0.0 阻塞)

§3.1 列了 OpenTelemetry。2.0.0 先做好结构化日志的 `request_id` 关联(等于轻量级 trace),完整分布式 trace(span / context propagation)后置到有 hosted 真实流量时再做。`request_id` 已经能覆盖单人排障的绝大多数场景。

### 9.6 2.0.0 范围

**2.0.0 必做**:

- structlog JSON + request_id 关联 + **强制脱敏 processor**(安全红线,不可后置)
- audit_logs 表 + §9.3.2 必审计事件(reveal_secret / AuthZ deny / SQL execute / AI call 等)
- Prometheus `/metrics` 基础指标(jobs / queue depth / 进程内存 / worker 健康)
- Portable 诊断包导出

**2.0.0 不做**:OTel 完整 trace、内置告警引擎、审计防篡改哈希链、日志聚合 UI。

### 9.7 待定 / 待讨论

- 审计日志留存期具体值(取决于金融客户合规要求,GA 前按客户定)
- hosted 用腾讯云 CLS/Prometheus 托管 vs 自管 Loki/Grafana(取决于自管运维负荷,见 §3.5)
- 审计日志是否需要导出为客户审计系统标准格式(syslog / CEF)

---

## 10. 版本迭代方案

### 10.1 版本号策略

| 版本 | 说明 |
|---|---|
| 2.0.0 | 第一个端到端骨架,功能不超过 1.x 50%,架构完整即可 |
| 2.x.y | minor = 新能力域,patch = bug fix |
| 2.x-portable.win / 2.x-docker | 形态特定产物 |
| 1.x.y | 1.x LTS(含明文密码 hotfix) |

### 10.2 Release Roadmap

| 版本 | 主题 | 关键交付 | 周期 |
|---|---|---|---|
| 1.x hotfix | 安全补丁 | 明文密码加密 + 自动迁移 + gitleaks | 1-2 天 |
| **2.0.0** | 骨架版 | 登录 / datasource / SELECT 基础 / **PG 全形态 + Portable launcher** / **API-Worker 分离 + PG queue** / SecretStore(两层)/ License skeleton + Repair Mode / AI Gateway 壳 / migrate_from_v1.py / Adapter(MySQL+DM Certified,Oracle 2.0.x 补,DB2 Preview) | 2-2.5 月 |
| 2.1.0 | SQL Workspace 完整 + AI Assist 一批 | ResultSet spool / virtual scroll / 历史 / 模板 / **AI Assist: 计划解释 / 错误翻译 / 改写** / DB2 转 Certified | 2.5 月 |
| 2.2.0 | Compare + AI Assist 二批 | 流式 + parquet + 异步导出 + 跨源 / 映射自动推断 + diff_profile + 递归 checksum 下钻 + 跨库规范化层 + 任务建议 / **AI Assist: 残余字段映射 + 差异归因** | 1.5 月 |
| 2.3.0 | Lineage(沿用 LineageReport) | 表级+列级血缘(沿用 1.x LineageReport)+ 子图优先图引擎 + 基础影响分析 + 解析覆盖率治理 + 资产页 + trace-compare / **AI Assist: 血缘解释 + 兜底解析** | 2 月 |
| 2.4.0 | Workflow(Job DAG) | DAG + 调度 + 节点白名单 + 通知 | 1 月 |
| 2.6.0 | AI Copilot MVP + Scenario Lab | Copilot Loop(自写)+ Memory + **C1(Schema-aware SQL)** + **Scenario Lab 完整版**(三层 DSL: tables/anomalies/workloads + AI fill 一体;1.x 挖掘结论见 `docs/backlog.md`)+ C5(AI Scenario 脚手架与之合并) | 3.5 月 |
| (原 2.3.0 Scenario Lab 槽位) | — | **移至 2.6.0**(2026-06-19 重排):Scenario 的 AI fill 与 AI Copilot 一体,且 workload 依赖 Lineage(2.3)/Workflow(2.4),放 2.6 依赖全部就位。详见下方排期说明 | — |
| 2.7.0 | 多租户 Hosted + Copilot 进阶 | RLS + OIDC + 腾讯云自管部署 + **C2 + C4 + C5进阶 + C3** | 2.5 月 |
| 2.8.0+ | 反馈驱动 | — | 不定 |

总周期:1.x hotfix 后到 2.7.0 GA 约 16-18 个月。

### 10.3 2.0.0 范围明确

**必做(骨架)**:全形态 PG 启动(含 Portable launcher + Managed Local PG)/ **API-Worker 进程分离 + PG job queue** / 登录 + 用户 + 项目 + datasource CRUD / SQL Workspace 最基本 SELECT(走 spool,还不要求 virtual scroll/历史/模板)/ Job 抽象 + PG queue backend / SecretStore 两层完整 / **日志三管线(structlog 强制脱敏 + audit_logs + Prometheus 基础指标)** / AI Gateway 壳(接 1 provider 可调通)/ License skeleton + Repair Mode / migrate_from_v1.py(可跑通)/ Database Gateway + Adapter(MySQL+DM Certified,Oracle 允许 2.0.x 补,DB2 Preview;接口以 Oracle/DM 为假想验证对象)/ 基础前端。

**不做**:完整 SQL Workspace(→2.1)/ Compare(→2.2)/ Lineage(→2.3)/ Workflow(→2.4)/ Scenario(→2.6,与 Copilot 一体)/ AI Assist 具体功能(→2.1+)/ AI Copilot(→2.6)/ 多租户计费(→2.7)。

**成功标准**:portable 解压启动 → 登录 → 加 MySQL 数据源 → 跑 SELECT(经 worker + spool)→ 看到结果。docker compose(api+worker+pg)同流程跑通。hosted 单租户跑通。1.x 数据能 migrate 进来(允许个别字段失败)。

### 10.3.1 跨版本约束:AI 上下文接口前置(v0.3.2 新增)

AI Copilot 虽 2.6.0 才落地,但其依赖的"上下文接口"必须在更早能力域就埋好,否则 2.6.0 要返工改前面版本的 schema/接口。两条硬约束:

| 约束 | 在哪个版本埋 | 服务于哪个 demo |
|---|---|---|
| Database Gateway introspection 接口(schema/列/外键)设计干净、业务可复用,不只够前端用 | **2.0.0** | C1 Schema-aware SQL |
| Scenario 模板数据结构预留"供 AI 读真实数据分布"的字段/接口 | **2.3.0** | C5 基础版 Scenario 脚手架 |

做这两个版本时,即使当时还没有 AI,也要按"将来 AI 要读这些上下文"来设计接口。这是为了 2.6.0 不返工,不是 2.0.0/2.3.0 当下就做 AI 功能。

### 10.4 分支策略

```
main             ← 2.x 主线(feature flag 控制未完成功能)
└── feat/... fix/...
v1-lts           ← 1.x 维护(6 个月内)
└── fix/datasource-password-encryption(立刻)
```

约束:每 PR 有契约测试;schema 变则更新 migrate_from_v1.py;**CI 不跑 SQLite smoke(2.0 不支持 SQLite backend)**;每 PR 过 gitleaks + secret 检测;**每 PR 验证 API/worker 分离下 job 能正常入队消费**。

### 10.5 CI / 发布流程

```yaml
on_pr:
  - lint (ruff + mypy + ESLint + TS)
  - secret_scan (gitleaks + 自定义)
  - unit_tests (pytest + vitest)
  - integration_tests (compose: api+worker+pg, e2e)
  - migrate_dry_run (1.x 样本 → 2.0 PG)
  - workflow_whitelist_check (1.x workflow 含禁止节点应报错)
  - job_queue_test (多 worker claim 不冲突)
on_release_tag:
  - docker image / Windows portable zip(含 PG + Python runtime + launcher)/ tarball
  - License 私钥签 release manifest
  - GitHub Release + 文档站点
```

### 10.6 1.x EOL 时间表

| 时间点 | 1.x 状态 |
|---|---|
| 2.0.0 alpha/beta | 正常维护,不劝退 |
| **2.0.0 GA(前提:migrate_from_v1.py 稳定 + 2.0 覆盖 1.x 核心场景)** | 进入 LTS,只接 critical security fix |
| GA + 3 个月 | 不再接 bug fix,只 critical CVE |
| GA + 6 个月 | EOL,推荐全部迁 2.0 |

**前提条件(v0.3.1 新增)**:6 个月窗口从"migrate_from_v1.py 稳定 **且** 2.0 覆盖 1.x 核心使用场景"之时起算,不从名义 GA 起算。否则 2.0 名义发布但迁移工具不稳会很尴尬。单人项目 12 个月双维护不可持续,6 个月为上限。

### 10.7 ADR 列表

```
docs/adr/
├── 0001-postgres-as-unified-metadata-store.md
├── 0002-sqlite-rejected-for-runtime.md
├── 0003-portable-runtime-supervisor.md            (v0.3.1 更新:从 managed-pg 扩为 launcher)
├── 0004-no-jdbc-in-2.0-main-line.md
├── 0005-resultset-spool-no-long-lived-cursor.md    (v0.3.1 新)
├── 0006-on-prem-as-primary-deployment.md
├── 0007-secret-bootstrap-vs-application.md         (v0.3.1 新,取代旧 unified-secret-store)
├── 0008-no-plaintext-secrets-anywhere.md
├── 0009-workflow-as-job-dag.md
├── 0010-ai-three-tier-architecture.md
├── 0011-ai-agent-deferred-to-2.6.md
├── 0012-offline-license-with-repair-mode.md        (v0.3.1 更新)
├── 0013-lineage-reuse-v1-lineagereport.md          (v0.3.2 改:推翻 aspect 收敛)
├── 0014-api-worker-separation-pg-queue.md          (v0.3.1 新)
├── 0015-adapter-capabilities-and-tiers.md          (v0.3.1 新)
├── 0016-ai-data-egress-policy.md                   (v0.3.1 新)
├── 0017-three-pronged-logging-runtime-audit-metrics.md  (v0.3.2 新)
├── 0018-worker-heartbeat-timeout-slow-olap.md      (v0.3.2 新)
├── 0019-lineage-subgraph-first.md                  (v0.3.3 新)
└── 0020-lineage-persist-edges-sql-hash.md          (v0.3.3 新)
(废弃) 0099-DEPRECATED-portable-sqlite-default.md
```

### 10.8 Charter 关系

Charter 3-4 页,只放:一句话定位 / 三形态对照 / 不做清单(SQLite 拒绝、Workflow 白名单、AI Agent 后置、商业模式不做)/ 与 1.x 关系 + EOL 表 / Release 节奏。本文档(技术方案)持续更新。ADR 永不修改(只能 Deprecated / Superseded)。

**下一版应为 Charter + 本技术方案定稿。**

---

## 附录 A:术语表

| 术语 | 含义 |
|---|---|
| 形态 | Hosted / On-prem Docker / Windows Portable |
| 能力域 | SQL Workspace / Compare / Scenario Lab / Lineage |
| Job | 统一长任务抽象 |
| ResultSet | 一次执行的结果,数据在本地 spool,不持有 DB cursor |
| spool | worker 流式写入的本地结果文件(parquet/arrow) |
| launcher | Portable 轻量进程,管 PG+API+worker 生命周期(非高可用) |
| Bootstrap Secret | 连 PG 之前需要的密钥(master key / PG 密码 / license),文件系统管理 |
| Application Secret | 系统起来后管理的密钥(datasource pwd / AI key 等),进 PG secret_refs |
| AI Gateway / Assist / Copilot | AI 三层(基础设施 / 常驻 / 商业差异化) |
| Egress Level | AI 数据出站分级 L0-L5 |
| AdapterCapabilities | adapter 声明支持的能力集合 |
| Certified / Preview | adapter 成熟度等级 |
| 运行日志 / 审计日志 / 指标 | 日志三管线:排障 / 合规 / 可观测,分离设计 |
| 脱敏 processor | structlog 全局拦截器,日志写出前强制移除敏感值 |
| request_id | 贯穿 API→Worker 的关联 ID,串联一次请求的所有日志与审计 |

---

## 附录 B:决策表(v0.3.3)

状态定义:**Accepted**(已敲定,charter 红线,变更需新 ADR)/ **Proposed**(方向定,细节待作者确认)/ **Deferred**(后续版本再定)/ **Open**(仍讨论)/ **Rejected**(明确不做)

| 编号 | 议题 | 状态 | 当前决策 | 阶段 | 阻塞 2.0.0 |
|---|---|---|---|---|---|
| B1 | AI Agent Loop | **Deferred** | 2.6 前不决策;倾向自写极简 | 2.6.0 | 否 |
| B2 | Server State | Accepted | 新模块 TanStack Query,1.x 复用模块保留 Pinia,不专门迁移 | 2.0+ | 部分 |
| B3 | Workflow API | Accepted | Job DAG + 节点白名单(ADR-0009,2026-07-02 补写并收录首版拍板:进程内 cron tick / 简单 RetryPolicy / when 旁路 / 首版开放 5 节点) | 2.4.0 | 否 |
| B4 | Lineage 结构 | Accepted | 沿用 1.x LineageReport(20字段),实现按版本分批;无 aspect 收敛(伪命题已推翻);**持久化部分由 ADR-0020 修订为边表持久化 + sql_hash 缓存** | 2.3.0 | 否 |
| B5-L | Lineage 子图优先(任务书 B5;历史 B5 已占用) | **Accepted** | 一等公民查询为焦点表 N 跳邻域子图;按需计算/加载;禁止全图计算后过滤;全景图仅作惰性 + 分层聚类次要视图 | 2.3.0 | 否 |
| B6-L | 基础影响分析前移(任务书 B6;历史 B6 已占用) | **Accepted** | 纯图反向遍历输出下游波及清单(按深度分层),提前到 2.3.0;C3 保留为 2.7.0 的 AI 加权增强 | 2.3.0 | 否 |
| B7-L | Compare 递归下钻 + 规范化层(任务书 C;历史 B7 已占用) | **Accepted** | 递归 hashdiff 下钻替代固定分块;跨库规范化 SQL + 逐行 MD5 截 8 字节 SUM 为跨库比对基线;采样快检只输出置信度差异率上界 | 2.2.0 | 否 |
| B5 | Hosted 云 | **Accepted** | 接口中立 + 实现单云:绑腾讯云,全程自管(CVM+Docker,复用 on-prem),AI 辅助运维 | 2.7.0+ | 否 |
| B6 | AI Copilot Demo | **Accepted** | C1-C5 确认;优先级 1/5/2/4/3;2.6.0=C1+C5基础版,2.7.0=C2/C4/C5进阶/C3 | 2.6-2.7 | 否 |
| B7 | 商业模式 | **Deferred** | 2.0 阶段不做;§9 已删 | GA 前 | 否 |
| B8 | 多租户隔离 | Accepted | hosted MVP app 层 + quota,RLS 后置 | 2.7.0 | 否 |
| B9 | Portable backend | **Accepted** | Managed Local PostgreSQL(launcher 托管),SQLite Rejected | 2.0.0 | 是 |
| B10 | AI 历史导入导出 | **Deferred** | 先定 JSON 格式,UI 后置 | 2.6.0 | 否 |
| B11 | License 验证 | Accepted | Ed25519 离线签名,grace 7 天,失败进 Repair Mode,不联网不硬件绑定 | 2.0.0 | 是 |
| B12 | 1.x EOL | Accepted | GA 后 6 个月,前提:迁移稳定 + 覆盖核心场景 | 2.0.0 GA | 是 |
| B13 | SecretStore | Accepted | 两层(Bootstrap / Application),CI lint 强制 | 2.0.0 | 是 |
| B14 | Hosted 目标云 | **Accepted** | 腾讯云(作者已有 CVM);托管产品按需靠适配器接 | 2.7.0 | 否 |
| B15 | 内嵌 PG 版本 | Accepted | PG16 initially,GA 前评估 PG17 | 2.0.0 | 是 |
| B16 | API/Worker 模型 | **Accepted** | on-prem/hosted 分离 + PG queue;portable launcher 管;dev thread | 2.0.0 | 是 |
| B17 | ResultSet cursor | **Accepted** | spool 模式,不长期持有 DB cursor | 2.0.0 | 是 |
| B18 | Adapter 范围 | **Accepted** | GA Certified: MySQL+DM;2.0.x: Oracle(移植不卡GA);2.1.0: DB2;推进序 MySQL→DM→Oracle→DB2;能力矩阵 | 2.0.0 | 是 |
| B19 | AI Egress | **Accepted** | L0-L5 分级,L5 永禁,L4 默认禁需 opt-in;C2/C5 high-risk | 2.0.0(Gateway 检查) | 是 |
| B20 | 日志体系 | **Accepted** | 三管线分离:运行日志(structlog+request_id+强制脱敏)/ 审计(PG表)/ 指标(Prometheus);脱敏 processor 为安全红线 | 2.0.0 | 是 |

**已全部 Accepted**:B5-L / B6-L / B7-L(本轮 Compare/Lineage 调研修订),B5 / B6 / B14(此前收敛)。
**已 Deferred**:B1(Agent 框架)/ B7(商业模式)/ B10(AI 历史导入导出)。
**无 Open 项**。

---

**文档版本**:v0.3.3
**已决议红线**:PG 统一 / API-Worker 分离 + PG queue / ResultSet spool 不持 cursor / SecretStore 两层 / 日志强制脱敏(三管线分离)/ Workflow 白名单 / License Repair Mode / AI Egress L0-L5 / Adapter 分级(MySQL+DM GA, Oracle 2.0.x, DB2 2.1)/ Hosted 腾讯云自管 / AI Copilot C1-C5 优先级 1/5/2/4/3 / 1.x 6 个月 EOL(带前提)/ 商业模式不做
**附录 B 全部 Accepted 或 Deferred,无 Open 项**
**下一步**:产出精简 Charter(3-4 页)+ 本文档定稿
