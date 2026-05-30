# DataOpsStudio 2.0 — Agent 开发契约(AGENT_CONTRACT v2)

> **这是给 AI 编码 agent(Codex / Claude Code)看的执行契约,不是设计稿。**
> 完整设计见 `docs/design/dataops-2.0-tech-design-v0.3.2.md`(1806 行)。
> 本文件只保留写代码时**必须遵守的硬约束**。两个 agent 共享此契约。
>
> **三层文档结构**:
> - **`AGENT_CORE.md`**(自动加载,< 12KB):速记 + 索引,30 秒看完
> - **本文件**:执行契约,完整规则定义,**开工前必读**
> - **`docs/agent-playbook.md`**:历史 changelog + 踩坑案例 + 设计背景,按需查
>
> **角色分工**:
> - **Codex = 主力**,写后端实现(adapter / API / worker / job backend / secretstore / license / migrate)
> - **Claude Code = 辅助 + 前端**,定接口契约 / 写 CI 红线 / review Codex 产出 / 守架构一致性 / **做前端 UI(T7,见 §10)**
> - **人(项目所有者)= 最终架构裁决 + 合并把关 + UI 视觉定版**

---

## 0. 黄金法则(违反即拒绝合并)

1. **接口先行**:Step 1 冻结的 Protocol 接口签名是契约,实现必须匹配。变更分两类,区别看"会不会弄坏另一方已写的代码":
   - **纯追加(向后兼容)**:新 method / 新 enum 值 / 新可选字段 — 不会弄坏既有代码。**可直接做,但 commit message 必须明示** `接口追加:xxx`(便于另一方扫 git log 知道契约动了哪),且**同步更新契约 §3 对应章节**。先做后通知 OK。
   - **破坏性变更**:改签名 / 删除 / 改类型 / 改语义 — 会弄坏既有代码。**必须先改契约 + 通知另一 agent → 等通知确认 → 才改代码**。直接改 = 拒绝合并。
   - 不确定哪类时,按"破坏性"处理,先问。
2. **物理隔离**:并行任务不得同时改同一文件/同一目录。见 §6 任务拆分。
3. **红线是 CI,不是自觉**:§4 的红线全部有对应 CI 检查,提交即拦。
4. **范围纪律**:只做 §5 列的 2.0.0 骨架内容。其他一律不做,即使"顺手能写"。
5. **不臆造**:设计稿没写的,先问人,不要自由发挥补设计。

---

## 1. 技术栈(不得替换)

- 语言:Python 3.12+
- Web:FastAPI + Pydantic v2 + Uvicorn
- ORM:SQLAlchemy 2 Core(不用 ORM session 模式,用 Core)
- 迁移:Alembic
- **元数据库:PostgreSQL 16+(唯一,禁止引入 SQLite 作为 runtime backend)**
- 队列:PostgreSQL job queue(默认)
- 日志:structlog
- 指标:prometheus_client
- SQL 解析:sqlglot
- 数据:pyarrow + pandas(ResultSet spool 用 parquet/arrow)
- Secret 加密:cryptography(Fernet) + bcrypt
- License 验签:PyNaCl(Ed25519)
- 前端:Vue 3 + TS + Vite + Pinia + TanStack Query + CodeMirror 6

---

## 2. 目录结构(谁放哪,不得乱放)

```
app/
├── main.py                  # API 进程入口
├── launcher.py              # Portable launcher(管 PG+API+worker 生命周期)
├── worker.py                # Worker 进程入口
├── api/
│   ├── routes/              # FastAPI router(按能力域分组)
│   └── middleware/          # RequestId / AuthN / License / RateLimit / Audit
├── services/                # 业务逻辑(★ 禁止 import 数据库驱动)
├── dbclients/               # DatabaseAdapter 实现(✓ 唯一可 import 数据库驱动处)
├── infrastructure/          # 基础设施实现
│   ├── jobbackend/          # PostgresJobBackend / ThreadPoolJobBackend
│   ├── resultstore/         # LocalFsResultStore / S3ResultStore
│   ├── secretstore/         # ✓ 唯一可 import Fernet/bcrypt 处
│   ├── bootstrap/           # BootstrapSecrets(读文件,不走 PG)
│   └── license/             # Ed25519 验签
├── domain/                  # 领域模型(Job / ResultSet / WorkflowSpec 等 dataclass)
├── observability/
│   └── logging.py           # structlog 配置 + ★ 脱敏 processor
├── config.py                # pydantic-settings
└── db/
    ├── models.py            # SQLAlchemy Core 表定义
    └── migrations/          # Alembic

frontend/                    # Vue 3 SPA
tests/
├── e2e/                     # ★ 2.0.0 验收测试(见 §5)
├── contract/                # 接口契约测试
└── unit/
.github/workflows/
└── ci.yml                   # ★ 含红线 lint(见 §4)
docs/
├── design/                  # 技术方案 v0.3.2
└── adr/                     # ADR
```

★ = 安全/架构关键点,review 必查。

---

## 3. 核心 Protocol 接口(Step 1 由 Claude Code 定,冻结后 Codex 实现)

> 以下签名为契约。实现类必须匹配。`ResultRef` / `Row` / `Column` / `ResultProfile` 等类型在 `app/domain/` 定义。

### 3.1 JobBackend(`app/infrastructure/jobbackend/`)

```python
class JobBackend(Protocol):
    def enqueue(self, job: Job) -> None: ...
    def claim_next(self, worker_id: str) -> Optional[Job]: ...   # FOR UPDATE SKIP LOCKED
    def complete(self, job_id: str, result_ref: ResultRef) -> None: ...
    def fail(self, job_id: str, error: str) -> None: ...
    def request_cancel(self, job_id: str) -> None: ...
    def heartbeat(self, job_id: str, worker_id: str) -> None: ...

# 2.0.0 实现:PostgresJobBackend(默认), ThreadPoolJobBackend(dev/极简portable)
# 不做:RedisRqJobBackend(hosted scale-out,后置)
```

### 3.2 DatabaseAdapter(`app/dbclients/`)

```python
class DatabaseAdapter(Protocol):
    capabilities: AdapterCapabilities       # 必须声明,调用方按此降级

    def execute_select(self, sql: str, params: dict) -> Iterator[Row]: ...  # 流式
    def explain(self, sql: str) -> PlanNode: ...
    def stream_rows(self, sql: str) -> Iterator[Row]: ...
    def test_connection(self) -> bool: ...
    def list_schemas(self) -> list[Schema]: ...
    def list_tables(self, schema: str) -> list[Table]: ...
    def list_columns(self, schema: str, table: str) -> list[Column]: ...
    def list_indexes(self, schema: str, table: str) -> list[Index]: ...
    def get_table_ddl(self, schema: str, table: str) -> str: ...
    def kill_query(self, connection_id: str) -> bool: ...

class AdapterCapabilities:
    execute_select: bool
    explain: bool
    stream_rows: bool
    server_side_cancel: bool
    list_schemas: bool
    list_tables: bool
    list_columns: bool
    list_indexes: bool
    get_table_ddl: bool
```

**2.0.0 adapter 推进顺序**:MySQL(1,骨架验证)→ DM(2)→ Oracle(3,可滑 2.0.x)→ DB2(4,Preview)。
**骨架设计约束**:首个写 MySQL,但接口设计必须以 **Oracle/DM 为假想验证对象**——防 MySQL-specific 假设固化:标识符大小写(Oracle 默认大写)、分页语法(LIMIT vs ROWNUM/FETCH FIRST)、cursor 语义、类型映射。

#### 3.2a Adapter 入参契约:DatasourceConnInfo(`app/domain/datasource.py`)

所有 DatabaseAdapter 实现的构造器接受统一的 DatasourceConnInfo 入参。1 个实例 = 1 行 datasources PG 表。这避免每个 adapter 自定义构造签名导致的:
- 调用方代码与每个 adapter 耦合
- adapter 工厂 / 注册表难以写
- datasources 表 schema 变更需要改每个 adapter

```python
class DbType(StrEnum):
    MYSQL = "mysql"
    ORACLE = "oracle"
    DM = "dm"
    DB2 = "db2"
    POSTGRESQL = "postgresql"

class DatasourceConnInfo(BaseModel):  # Pydantic v2 BaseModel, ★ frozen=True
    host: str                  # min_length=1
    port: int                  # 1..65535
    username: str              # min_length=1
    database: str              # min_length=1;映射 datasources.database_name 列(SQL 保留字)
    password_ref: SecretRef    # ★ kind 校验 == DATASOURCE_PASSWORD(R4 一脉)
    db_type: DbType
    extra: dict[str, Any]      # adapter 私有(Oracle service_name / MySQL ssl_mode 等)
```

★ 持久化对应见 §5.1 datasources 表 + `app/db/models.py`。
★ `database` 字段:DB 列名为 `database_name`(`database` 是 SQL 保留字,做列名易埋坑),domain 字段叫 `database`,应用层做名字映射。
★ `extra` 子节存于 `datasources.capability_profile.connection`。

### 3.3 SecretStore + BootstrapSecrets(`app/infrastructure/secretstore/` + `bootstrap/`)

```python
# Application Secret:系统起来后管理,存 PG secret_refs 表,master key 加密
class SecretStore(Protocol):
    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef: ...
    def reveal_secret(self, ref: SecretRef) -> str: ...     # ★ 每次调用必审计
    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef: ...
    def delete_secret(self, ref: SecretRef) -> None: ...
    def hash_password(self, plaintext: str) -> HashedRef: ...
    def verify_password(self, plaintext: str, ref: HashedRef) -> bool: ...
    def rotate_master_key(self, new_key: bytes) -> RotationReport: ...

# Bootstrap Secret:连 PG 之前需要,读文件系统,★ 绝不存 PG(否则启动循环)
class BootstrapSecrets(Protocol):
    def get_master_key(self) -> bytes: ...
    def get_pg_app_password(self) -> str: ...
    def get_pg_superuser_password(self) -> str: ...
    def get_jwt_secret(self) -> str: ...          # T4 最小补充:API 启动验 JWT,不走 PG
    def get_license_file(self) -> Optional[bytes]: ...

class SecretKind(Enum):
    DATASOURCE_PASSWORD = "datasource_password"
    AI_API_KEY = "ai_api_key"
    MFA_TOTP_SEED = "mfa_totp_seed"
    OAUTH_TOKEN = "oauth_token"
    WEBHOOK_SECRET = "webhook_secret"
    SIGNED_URL_SECRET = "signed_url_secret"
```

**铁律**:PG 连接密码是 Bootstrap Secret,**永远不进 secret_refs 表**。启动顺序:读 master key(文件)→ 读 PG 密码(文件)→ 连 PG → 用 master key 解密 PG 里的 Application Secret。

### 3.4 ResultStore + ResultSet(`app/infrastructure/resultstore/`)

```python
class ResultStore(Protocol):
    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef: ...
    def append_spool(self, result_set_id: str, rows: list[Row]) -> None: ...  # 流式追加
    def get_manifest(self, run_id: str) -> Manifest: ...
    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]: ...
    def open_download(self, ref: ResultRef) -> BinaryIO: ...
    def delete_run(self, run_id: str) -> None: ...
    def gc_expired(self) -> int: ...

# 2.0.0:LocalFsResultStore。S3ResultStore 后置 hosted。
```

```python
# domain/result.py —— ★ ResultSet 不持有 DbCursor
class ResultSet:
    result_set_id: str
    execution_id: str
    storage_ref: ResultRef          # 指向本地 spool(parquet/arrow),不是 cursor
    columns: list[Column]
    loaded_rows: int
    total_rows: Optional[int]
    state: Literal["streaming", "complete", "failed", "closed"]
    created_at: datetime
    def fetch_range(self, offset: int, limit: int) -> list[Row]: ...  # 读 spool
    def request_count_total(self) -> int: ...
```

### 3.5 AiGateway(`app/services/ai/`)— 2.0.0 只做壳

```python
class AiGateway(Protocol):
    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse: ...
    def stream_complete(self, ...) -> Iterator[AiChunk]: ...
# 2.0.0:能接 1 个 provider 调通即可。Assist/Copilot 功能后续版本。
# 但 ★ egress 检查 + 脱敏 + 审计的钩子位 2.0.0 就要留好。
# 实现:DefaultAiGateway(AiGateway),壳级实现,2.0.0 验证 provider 可调通。
```

### 3.6 Job 领域模型(`app/domain/job.py`)

```python
class Job:
    id: str
    kind: Literal["sql_query","test_connection","sql_explain","compare_run","export_excel",
                  "scenario_materialize","scenario_run_all","workflow_run",
                  "ai_assist_call","ai_copilot_run","lineage_analyze"]
    status: Literal["pending","running","success","failed","cancelled","timeout"]
    owner_user_id: str
    project_id: str
    datasource_ids: list[str]
    priority: int
    timeout_seconds: int
    resource_profile: ResourceProfile          # 见 app/domain/resource.py;2.0.0 字段:memory_limit_mb / timeout_seconds(均 Optional[int])
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

---

## 4. 红线(全部有 CI 检查,违反即拦)

| # | 红线 | CI 检查方式 |
|---|---|---|
| R1 | `services/` 禁止 import 数据库驱动(psycopg/oracledb/pymysql/dmPython/ibm_db) | AST/grep:驱动 import 只允许 `dbclients/` + `infrastructure/` |
| R2 | 业务代码禁止持有明文密码;只能持 SecretRef | grep `ds["password"]` / `ds.get("password")` / `.password =` 在 services 出现即 fail |
| R3 | Fernet/bcrypt/hashlib 只允许在 `infrastructure/secretstore/` | import 越界检测 |
| R4 | PG 连接密码禁止写入 secret_refs 表 | 代码审查 + 测试:store_secret 不接受 PG_*_PASSWORD kind |
| R5 | 日志禁止出现敏感值 | structlog 脱敏 processor(强制)+ 测试:打印含 password 的 dict 输出必须 REDACTED |
| R6 | ResultSet 禁止持有 DbCursor | grep `ResultSet` 类定义不得有 cursor 字段;cursor 持有时长 ≤ cursor_max_hold_seconds |
| R7 | Workflow 节点必须在白名单内 | 校验 job_kind ∈ ALLOWED_WORKFLOW_NODE_KINDS;禁 shell/python/任意HTTP |
| R8 | 配置文件禁止明文密码字段 | gitleaks + `config/*.json|yml` 出现明文 `password:` fail |
| R9 | CI 不跑 SQLite backend(2.0 不支持) | 测试矩阵只有 PG |
| R10 | f-string 拼接 `uuid4().hex` / `str(uuid4())` 禁止(拼前缀让 id 长度不可预测,超 schema 字段) | ast-grep:`tools/lint/rules/r10_no_uuid_prefix_concat.yml`(触发案例见 `docs/agent-playbook.md §2`) |

**ALLOWED_WORKFLOW_NODE_KINDS**(R7):
```python
{"sql_query","sql_explain","compare_run","scenario_materialize",
 "scenario_run_all","lineage_analyze","export_excel","notify","sleep","branch"}
```

**脱敏 processor(R5,Step 1 优先做)**:
- 按 key 名拦截:含 password/secret/token/api_key/mfa/authorization/cookie/ciphertext → `***REDACTED***`
- SQL 不记原文,记 sql_hash + sql_len
- 样本数据(rows/sample)永不进日志,记 rows_omitted
- 正则兜底:身份证/手机号模式打码

---

## 5. 2.0.0 范围(骨架版,严守边界)

### 必做
- 全形态 PG 启动(Portable launcher + Managed Local PG)
- **API-Worker 进程分离 + PG job queue**
- 登录 + 用户 + 项目 + datasource CRUD
- SQL Workspace **最基本** SELECT(走 worker + spool;**不要求** virtual scroll/历史/模板)
- Job 抽象 + PG queue backend
- SecretStore 两层(Bootstrap / Application)完整
- **日志三管线**:structlog 强制脱敏 + audit_logs 表 + Prometheus 基础指标
- AI Gateway 壳(接 1 provider 可调通,留 egress/脱敏/审计钩子)
- License skeleton + Repair Mode
- migrate_from_v1.py(可跑通,允许个别字段失败)
- Database Gateway + adapter:MySQL+DM Certified,Oracle 可滑 2.0.x,DB2 Preview
- 基础前端(登录/项目/datasource/SQL控制台/任务列表)

### 明确不做(写了也会被拒)
- 完整 SQL Workspace(virtual scroll/历史/模板)→ 2.1
- Compare → 2.2 / Scenario → 2.3 / Lineage → 2.4 / Workflow → 2.5
- AI Assist 具体功能 → 2.1+ / AI Copilot → 2.6
- 多租户 RLS / 计费 → 2.7
- Redis backend / S3 ResultStore / OTel 完整 trace / 告警引擎
- SQLite runtime backend(永不)

### ★ 跨版本预留(2.0.0 就要埋,防 2.6.0 返工)
- Database Gateway introspection 接口设计干净可复用(服务于 2.6.0 C1 Schema-aware SQL)

### 验收标准(e2e,唯一目标)
```
portable 解压 → launcher 启动(PG+API+worker)→ 登录
  → 加一个 MySQL 数据源(密码经 SecretStore 加密)
  → 在 SQL 控制台跑一条 SELECT(经 worker 执行,结果写 spool)
  → 前端从 spool 分页看到结果
docker compose(api+worker+pg)跑通同样流程。
1.x 数据能 migrate 进 PG(允许个别字段失败)。
```
这条 e2e 通过 = 2.0.0 骨架完成。所有任务最终服务于此。

### ★ 部署 / 冷启动验证约定(模式二:云服务器)

2.0.0 dogfood / 部署默认目标是**云服务器**,不是开发者本地机器。冷启动验证
(`bootstrap init → pg-up → alembic-up → admin create → API+worker → SELECT 1+1`)
和实际部署都在云服务器执行。

**登录与凭据来源**:
- agent 通过专用受限部署账号登录云服务器,**不是 root**。该账号权限只够部署
  DataOpsStudio,不得假设有无限系统权限。
- SSH host / user / key path / port 等登录凭据只从运行环境变量读取,例如:
  `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY_PATH` / `DEPLOY_PORT`。
- 凭据**绝对不进 GitHub / 代码 / 文档 / 配置 / commit**:
  服务器 IP、SSH key、账号、任何密码一律不得写进仓库任何文件。
- 部署脚本只能使用环境变量占位,如 `$DEPLOY_HOST`;禁止内联真实值。
- 不得把凭据写进日志、临时文件、注释、commit message;不得在代码/脚本中
  `echo` 出凭据。
- 每次 commit 前主动确认没有服务器信息混入。gitleaks 会扫,但 agent 不能只靠
  gitleaks 兜底。

**操作可见、可审查**:
- 部署前,agent 必须先告诉人"准备在云服务器执行哪些命令、做什么变更",等待确认
  后再执行。
- 禁止静默执行不可逆服务器操作,包括但不限于删数据、覆盖运行目录、改系统配置、
  改防火墙、安全组、systemd unit。
- 涉及删除/覆盖服务器已有内容的操作,必须先问人并获得明确确认。

**运行时 secret 边界**:
- DataOpsStudio 元数据库(PG)凭据由 launcher bootstrap 在服务器上生成。
- PG 凭据不得与云服务器登录凭据混用,不得带回本地,不得 commit。
- Bootstrap Secret 仍遵守 §3.3:连 PG 之前读文件系统,绝不存进 `secret_refs`。

**CI 边界**:
- GitHub Actions integration 测试仍使用 Actions 临时 service container。
- CI 不连接云服务器,也不读取 `DEPLOY_HOST` / `DEPLOY_USER` /
  `DEPLOY_SSH_KEY_PATH` 等部署凭据。

---

## 6. 任务拆分与并行规则

### Step 1(Claude Code 先行,串行,不可并行)
产出"围栏",冻结后才能并行:
- `app/domain/` 全部领域模型(Job/ResultSet/接口用到的类型)
- §3 全部 Protocol 接口定义(只签名,no impl,标 `...` 或 `raise NotImplementedError`)
- `app/observability/logging.py` 脱敏 processor(R5,先做)
- `app/config.py` pydantic-settings
- `app/db/models.py` + Alembic 首个 migration(users/projects/project_members/datasources/jobs/job_events/secret_refs/audit_logs/result_sets/license_state)
- `.github/workflows/ci.yml` 红线 lint(R1-R9)
- `tests/contract/` 接口契约测试骨架
- `tests/e2e/` 验收测试骨架(先红)

### Step 2(Codex 主力,可并行,物理隔离)
接口冻结后,以下任务块**目录不重叠,可并行**:

| 任务块 | 目录 | 依赖 / 归属 |
|---|---|---|
| T1 PG JobBackend + worker | `infrastructure/jobbackend/` + `worker.py` | Step1 接口;Codex |
| T2 MySQL adapter + spool | `dbclients/mysql_adapter.py` + `infrastructure/resultstore/` | Step1 接口;Codex |
| T3 SecretStore 两层 | `infrastructure/secretstore/` + `bootstrap/` | Step1 接口;Codex |
| T4 API + middleware | `api/` | Step1 接口;Codex |
| T5 License + Repair Mode | `infrastructure/license/` | Step1 接口;Codex |
| T6 launcher | `launcher.py` | T1/T4 接口;Codex |
| T7 前端 | `frontend/` | T4 的 API 契约;**★ Claude Code 负责(非 Codex),按 §10 视觉规范** |
| T8 migrate_from_v1 | `tools/migrate_from_v1.py` | Step1 models;Codex(参考 1.x:见 `docs/agent-playbook.md §3` 设计背景)|

**并行铁律**:同一时间,一个任务块只由一个 agent 负责,不得两个 agent 同改一块。

### Step 3(拼装 + e2e)
- 接线:API enqueue → worker claim → adapter 执行 → spool → 前端读
- 跑通 §5 验收 e2e
- Claude Code 全量 review:R1-R9 红线 + 接口一致性 + 命名/错误处理/日志风格统一

### Review 节奏(贯穿 Step 2/3)
Codex 每完成一个任务块 → Claude Code review:
1. 接口签名是否与 §3 契约一致?
2. 是否触碰 R1-R9 红线?
3. 是否越界做了 §5 不做项?
4. 命名/错误处理/日志打法是否与其他块一致?
不过 → 退回 Codex 改。

---

## 7. 给 Codex 下任务时的话术模板

每个任务块给 Codex 的 prompt 应包含:
```
1. 读 AGENT_CONTRACT.md §3(你要实现的接口)+ §4(红线)+ §5(范围)
2. 你的任务:实现 [T?],只动 [目录],不碰其他目录
3. 必须匹配 §3 的接口签名,不得改签名
4. 不得触碰 §4 红线(尤其 R?/R?)
5. 不得做 §5 不做项
6. 完成后写 contract test 证明接口匹配
7. 有疑问先问,不要臆造设计
```

---

## 8. 这份契约的维护

- 接口要改 → 先改本契约 §3 → 通知两个 agent → 再改代码
- 本契约与 v0.3.2 设计稿冲突时,**以设计稿为准**,并修正本契约
- 本契约只是设计稿的"执行摘要",不是新决策来源

---

## 10. 前端视觉规范(T7,Claude Code 负责)

> 这是**第一版默认值**,定版靠真实界面迭代。Claude Code 先做 design system 预览页,人在浏览器看效果后调整。不走 Figma。

### 10.1 整体定位

**"清爽外壳 + 致密工作区",天空蓝统一调性,浅色为主、SQL 工作区可选深色。** 气质参考:Linear 的克制 + Supabase 的专业数据感。

三条拍板决定(agent 不要推翻):
1. **密度分区**:外壳(登录/项目/数据源/设置)留白舒展;工作区(SQL/结果/血缘)信息密度优先,留白收紧。数据工具必须这样,否则不好用。
2. **浅色为主,SQL 编辑器/结果区默认可选深色**(DBA 久看护眼)。
3. **天空蓝只做强调色**,不大面积铺底(大面积蓝显廉价)。蓝只用于:主按钮、选中态、链接、focus 边框、品牌点缀。

### 10.2 设计 token

**色彩**
```
主色      sky-500   #0EA5E9    主按钮/选中/链接/focus
主色 hover sky-600  #0284C7
主色浅     sky-50    #F0F9FF    选中行背景/浅强调
外壳背景   slate-50  #F8FAFC
卡片       white + 1px slate-200 边框
文字主     slate-800 #1E293B    (不用纯黑)
文字次     slate-500 #64748B
边框       slate-200 #E2E8F0
成功 emerald-500 / 危险 red-500 / 警告 amber-500
工作区深底 slate-900 #0F172A    (SQL编辑器/结果区可选)
```

**形态**
```
圆角  8px(卡片/按钮) / 6px(输入框) / 4px(标签)
阴影  subtle: 0 1px 3px rgba(0,0,0,0.08)  —— 克制,不要重投影
分隔  1px solid slate-200 细线,不用重色块
间距  外壳 16-24px / 工作区 8-12px
```

**字体**
```
界面  系统字体栈(-apple-system, Segoe UI, ...)
代码/SQL/数据  等宽(JetBrains Mono / Cascadia Code / ui-monospace)
字号  界面 14px / 数据表格 13px / 标题 16-20px
```

### 10.3 分区设计

- **外壳**:左侧窄导航(图标+文字,选中态天空蓝),内容区大留白卡片式,表单舒展、输入框 focus 蓝边。
- **工作区**:SQL 编辑器 CodeMirror(可选深色 slate-900);结果表格 13px 等宽数字、行高紧凑、斑马纹极淡、表头吸顶、虚拟滚动;密度优先但保持统一圆角和蓝强调。
- **状态色**:Job 状态用小圆点+文字——pending 灰 / running 蓝(脉冲动画)/ success 绿 / failed 红 / cancelled 灰。不用大色块。

### 10.4 signature 细节(提气质、防模板感)

主按钮 / 品牌 logo / 空状态插画用一点 sky-400→sky-600 微渐变;其他地方纯色克制。一点点渐变即可,多了俗。

### 10.5 工作流

1. Claude Code 先做 **design system 预览页**:把色彩/按钮/输入框/表格/状态点/卡片全做出来排一页。
2. 人 `npm run dev` 浏览器看真实效果,调 token。
3. 视觉定版后再做 §5 范围内的具体页面(登录/项目/datasource/SQL控制台/任务列表)。
4. **不要超出 §5 的 2.0.0 前端范围**(别因追求好看把 2.1+ 功能页提前做)。

### 10.6 技术约束(同设计稿 §3.2 前端栈)

Vue 3 + TS + Vite + Pinia + TanStack Query(server state)+ CodeMirror 6 + Tailwind CSS v3 + lucide-vue-next + vue-i18n。**不引入设计稿未列的 UI 库**(如要引入组件库,先问人)。
