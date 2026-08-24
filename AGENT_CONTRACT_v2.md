# DataOpsStudio 2.0 — Agent 开发契约(AGENT_CONTRACT v2)

> **这是给 AI 编码 agent(Codex / Claude Code)看的执行契约,不是设计稿。**
> 完整设计见 `docs/design/dataops-2.0-tech-design-v0.3.2.md`(1806 行)。
> 本文件只保留写代码时**必须遵守的硬约束**。两个 agent 共享此契约。
>
> **三层文档结构**:
> - **`AGENTS.md` / `CLAUDE.md`**(agent 启动自动加载,~5.5KB):宪法 + 速记
> - **本文件**:执行契约,完整规则定义,**开工前必读**
> - **`docs/agent-playbook.md`**:历史 changelog + 踩坑案例 + 设计背景,按需查
>
> `AGENTS.md` 与 `CLAUDE.md` 内容除首行标题外**一字一致**,CI `Agents Doc Sync` job 守护。
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
│   └── interactive/         # InteractiveConnection 会话式新缝(见 §3.8),不扩展 DatabaseAdapter
├── broker/                  # Session Broker(API 进程内组件,console 会话所有权;★ 禁止 import
│                            #   数据库驱动,只 import dbclients seam。见 §3.8 / ADR-0023)
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
desktop/                     # Win/Linux 共用 Tauri 壳(平台差异限 cfg(target_os))
updater/                     # 完整包内稳定 payload updater(app/frontend 签名增量包)
bundle-scripts/              # Win .ps1/.cmd + Linux .sh,同一离线运行契约
tools/package/               # 两平台可复现构建器(下载必须 sha256 pin)
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

#### 3.2-ColumnType:跨方言统一列类型(`app/domain/schema.py`)

`Column.type` 是**跨方言统一枚举 `ColumnType`**(StrEnum),不是 driver-specific 字符串。各 adapter 把自己的 driver 类型码(PyMySQL FIELD_TYPE 整数 / dmPython type code / Oracle DATA_TYPE 字符串…)**映射到这一套统一值**,前端按一套语义染色 / cast / format,不做 N 方言条件分支。原始 driver 类型字符串放 `Column.driver_type: str | None` 备查。

```python
class ColumnType(StrEnum):
    STRING = "string"        # char/varchar/text/clob/enum/set
    INTEGER = "integer"      # tinyint..bigint / year / bit(int)
    FLOAT = "float"          # float/double/real/binary_float
    DECIMAL = "decimal"      # decimal/numeric/Oracle&DM NUMBER(无 scale 信号保守归此)
    BOOLEAN = "boolean"      # bool/boolean(MySQL tinyint(1) 无可靠信号 → 仍归 INTEGER)
    DATETIME = "datetime"    # datetime/timestamp
    DATE = "date"
    TIME = "time"
    BYTES = "bytes"          # binary/varbinary/blob/raw/image
    JSON = "json"
    UNKNOWN = "unknown"      # 无法识别的 driver 类型码(不臆造;留 driver_type 供人工核对)

class Column(BaseModel):
    name: str
    type: ColumnType = ColumnType.UNKNOWN
    driver_type: str | None = None       # 原始 driver 类型,如 "VARCHAR(64)" / "NUMBER(10,2)"
    nullable: bool = True
    primary_key: bool = False
```

**映射约束**:
- 无法识别的类型码 → `UNKNOWN`(不猜),且必须填 `driver_type` 供人工核对。
- 映射逻辑放各 adapter 旁的 `<dialect>_types.py`(R1:仍在 `app/dbclients/`),单测覆盖每条映射。
- ★ **breaking change(前端契约)**:`/result` & introspection 响应里 `Column.type` 值域从 driver 字符串(MySQL FIELD_TYPE 数字 / Oracle DATA_TYPE)变为上述 11 个稳定值;新增 `driver_type` 字段。前端按枚举判断,不再 parse driver 串。**第二个 adapter 合并时强制同时引入**(backlog 硬规定),避免前端被迫做 N 方言分支。

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
    def snapshot_spool(
        self,
        source_result_set_id: str,
        snapshot_result_set_id: str,
    ) -> SpoolSnapshotManifest: ...  # 不可变独立副本;源删除后仍可读
    def mark_spool_truncated(self, result_set_id: str) -> None: ...  # 主动限行后幂等标记截断
    def set_spool_pagination(
        self,
        result_set_id: str,
        *,
        has_more: bool,
        mode: str,
        reason: str | None = None,
    ) -> None: ...  # 无 COUNT 的续页能力元数据;禁止放 cursor/凭据
    def get_manifest(self, run_id: str) -> Manifest: ...
    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]: ...
    def open_download(self, ref: ResultRef) -> BinaryIO: ...
    def delete_run(self, run_id: str) -> None: ...
    def gc_expired(self) -> int: ...

# 2.0.0:LocalFsResultStore。S3ResultStore 后置 hosted。
# ResultRef 允许携带 additive metadata dict,仅放轻量诊断/展示信息
# (如 connection_test 的 server_version / latency_ms、SQL 分阶段耗时),不得放敏感值或 cursor。
```

Compare 结果输入纯追加 interface:`CompareDataRef.kind="result_snapshot"` 只接受
`input_id` 与显式 `allow_partial`;该侧不绑定 datasource。创建/更新/运行均须通过
`CompareResultInputs.open()` 校验 actor + project 并续租,worker 只读 snapshot,
禁止重跑来源 SQL 或持有数据库 cursor。捕获请求的 `allow_partial` 默认为 false;
发现 `truncated/has_more` 时必须在 catalog 写入前拒绝并清理临时 snapshot,
只有显式 true 才能保留。取消/超时的 statement 即使底层 manifest 未标截断,
也必须按部分结果拒绝并在 descriptor 固化 `truncated=true,total_rows=null`。结果
列名必须唯一,否则同样在捕获边界拒绝并清理。

SQL 工作区把已落盘结果送入 Compare 时,浏览器一次性 handoff 只携带
`project_id`、`input_id` 与 `allow_partial`,不携带 SQL、结果行或数据库凭据;
Compare 必须按当前 project 重新读取 result input 描述后再填充草稿。截断/部分结果
必须由用户显式确认,且 handoff 与后续 `CompareDataRef` 均保留 `allow_partial=true`。

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
    kind: Literal[
        "sql_query",
        "test_connection",
        "sql_explain",
        "compare_run",
        "result_export",
        "export_excel",
        "scenario_materialize",
        "scenario_run_all",
        "workflow_run",
        "notify",
        "sleep",
        "branch",
        "workflow_sensor_check",
        "ai_assist_call",
        "ai_copilot_run",
        "lineage_analyze",
        "lineage_batch",
    ]
    status: Literal["pending","running","success","failed","cancelled","timeout"]
    owner_user_id: str
    project_id: str
    datasource_ids: list[str]
    priority: int
    # aware-only;当前 UTC 默认使旧调用方创建的 Job 立即可 claim
    # PostgreSQL 仅领取 status='pending' 且 available_at <= now() 的 Job
    available_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
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
    error_code: Optional[JobErrorCode]
    retry_count: int
    payload: dict
    parent_workflow_run_id: Optional[str]
```

### 3.7 Workflow 2.4.x 追加契约

```python
class WorkflowEdge:
    source: str
    target: str
    trigger: Literal["success", "failure"] = "success"
    when: Optional[str] = None
    is_default: bool = False

SUPPORTED_WORKFLOW_NODE_KINDS = {
    "sql_query", "sql_explain", "compare_run", "lineage_analyze",
    "export_excel", "notify", "sleep", "branch",
}
```

- 旧 `{source,target}` 边保持成功依赖语义。`branch` 与
  `on_failure="branch"` 均采用按边声明顺序 first-match、唯一 default 的路由。
- 节点 payload / `when` 可读取 `${nodes.<ancestor>.<field>}`,但仅限构造期验证过的
  拓扑上游和按节点 kind 声明的标量白名单。禁止结果行、ResultRef URI、SecretRef
  与任意 metadata 进入输出上下文。
- DAG Notify 的公开 payload 仅含 1..10 个唯一非空 target_ids 与可选
  message(最长 512 字符);RunNotification.message 与
  WorkflowNotifyService.notify_node 均为向后兼容的纯追加接口。
- Worker 必须通过 parent_workflow_run_id 读取冻结的父 workflow_run payload.spec
  解析 NotifyTarget,不得把目标配置或 SecretRef 复制进子 Job payload。DAG Notify
  尊重 target.enabled,但忽略 target.events(events 仅过滤 run 终态通知)。
- DAG Notify 按 target_ids 声明顺序尝试全部启用目标;仅当全部成功时子 Job 才成功,
  任一失败走既有节点 retry。投递发生在 backend.complete 之前,语义为 at-least-once,
  retry / stale worker 可重复投递;不承诺 exactly-once。
- DAG Notify 成功输出仅允许当前尝试的整数 sent_count;不得输出 ResultRef URI 或其它
  metadata。run 终态通知与 DAG Notify 隔离:终态通知失败不得改变 workflow_run 状态。
- `notify` 只引用 Workflow 已配置的通知 target id;`sleep` 只接受
  `duration_seconds`;`branch` payload 为空。Scenario 两种节点留待 2.6.0。
- Workflow 创建请求可选 `enabled: bool = true`,更新请求可选
  `enabled: bool | None = None`;省略更新字段时保留 `workflows.enabled`,显式布尔值才改列。
  `workflows.enabled` 是唯一权威来源,不得复制进 WorkflowSpec / `dag_jsonb`。
- 单 run 状态节点追加只读 `outputs`,值域仅 `str/int/float/bool/None`。kind-specific
  字段必须由 `extract_workflow_node_outputs` 从防御性解析的 ResultRef 投影,common
  `status/job_id/error_code` 来自权威 job 行;禁止 URI、任意 metadata、SQL/rows、
  SecretRef、容器值或未知字段。损坏 ResultRef 与损坏/缺失 spec fallback 均不得 500,
  且使用同一安全投影。run 历史列表保持轻量,不含节点 `outputs`。
- 接口追加: scheduler_timezone。全局 `DATAOPS_SCHEDULER_TIMEZONE` 只接受 IANA
  ZoneInfo key;缺省用 `tzlocal.get_localzone()` 解析服务器本地时区,显式非法值即使
  scheduler 禁用也必须拒绝启动。cron 按该进程级时区求值,fire point 转回 UTC 后
  才比较、入库、审计与构造 Job。修改配置需重启;不支持 per-workflow 时区或 backfill。

### 3.8 InteractiveConnection(`app/dbclients/interactive/`)— console 会话式新缝

> 会话式执行的**新 Protocol 新缝**,**不扩展 §3.2 DatabaseAdapter**:既有 adapter 与
> `AdapterCapabilities` 一律不动(job 路径行为零变化)。调用方是 `app/broker/`(API 进程内
> 组件)。决策、论证与取舍见 `docs/adr/0023-console-session-broker.md`(取代 ADR-0021)。

```python
class InteractiveCapabilities:          # 独立于 AdapterCapabilities,不合并
    server_cancel: bool                 # 控制连接可硬取消:DM / MySQL True
    server_statement_timeout: bool      # 服务端语句超时:MySQL True / DM False(客户端计时)
    session_streaming: bool             # execute 不缓冲、fetch 分批:True

class InteractiveConnection(Protocol):  # ★ threadsafety=1:只能由持有它的 lane 线程调用
    capabilities: InteractiveCapabilities
    session_marker: str                 # DM `SELECT SESSID()` / MySQL `CONNECTION_ID()`,数值标识非敏感

    def execute(self, sql: str, params: dict) -> None: ...
    def columns(self) -> list[Column]: ...                 # 类型映射沿用 §3.2-ColumnType
    def fetch_batch(self, size: int) -> list[Row]: ...     # 流式;批间由调用方查软取消 flag
    def set_statement_timeout(self, seconds: int | None) -> None: ...  # MySQL max_execution_time
                                                           # 保险带;None/0=关闭;DM 侧 no-op
    def rollback(self) -> None: ...
    def ping(self) -> bool: ...
    def close(self) -> None: ...

class CancelChannel(Protocol):          # 每 datasource 一条常驻控制连接,由 control lane 独占
    server_cancel: Literal["available", "degraded", "unknown"]

    def probe(self) -> str: ...                            # 对自身 marker 试调一次;权限错 → degraded
    def cancel(self, session_marker: str) -> None: ...     # SP_CANCEL_SESSION_OPERATION / KILL QUERY
    def destroy(self, session_marker: str) -> None: ...    # SP_CLOSE_SESSION / KILL CONNECTION
    def ping(self) -> bool: ...

class InteractiveErrorClass(StrEnum):
    STATEMENT_ERROR = "statement_error"    # 语句级错误,会话续用(DM -2104 / MySQL 1064,1146)
    CANCELLED = "cancelled"                # server_confirmed(DM -6515 / MySQL 1317)
    TIMEOUT = "timeout"                    # server_confirmed(MySQL 3024;DM 无,客户端计时归此)
    CONNECTION_DEAD = "connection_dead"    # → session_lost(socket 类 / MySQL 2006,2013)
    AUTH_FAILED = "auth_failed"            # 建连期(DM -2501 / MySQL 1045)
    HOST_UNREACHABLE = "host_unreachable"  # 建连期(DM -70028 / MySQL 2003,2005)

class ErrorClassifier(Protocol):
    def classify(self, exc: BaseException) -> tuple[InteractiveErrorClass, str]: ...
```

**约束(实现与 review 必查)**:

- ★ **只按数值错误码分类,永不按消息文本**——错误消息语言是生产环境配置项,文本匹配即误判。
- ★ 一条 `InteractiveConnection` 终身归单一 lane 线程与单一 console/属主:不跨线程共享、
  不跨 console 复用、**不入池**。连接不可续用即宣告 session_lost,不自动重连、不自动重放。
- ★ 建连超时按驱动**真实属性**接线:DM `login_timeout`(毫秒)、MySQL `connect_timeout`(秒);
  会话连接**不设** MySQL `read_timeout`(会误杀长查询)。旧 adapter 的错误接线在新缝内修正,
  不回改 job 路径。
- R1 不变:数据库驱动 import 仍只在 `app/dbclients/`;`app/broker/` 只 import 本 seam。
- R2 不变:seam 只接受 `SecretRef` / `DatasourceConnInfo`,明文只在 connect 内部短暂存在,
  无明文缓存;每次建连 reveal + 审计。
- R5 不变:seam 与 broker 的日志事件只出现 `sql_hash` 与长度,不得出现 SQL 原文、绑定参数、
  主机地址。
- R6 不变:cursor 只活在 lane 栈上,`ResultSet` / `ResultRef` 不持有 cursor。
- `CancelChannel.probe()` 结果缓存于控制 lane 生命周期;`degraded` 必须经 attach 响应如实
  透出给前端,不得静默降级为“看起来取消成功”。

### 3.9 PayloadUpdater(`updater/`)— 桌面载荷增量更新第一阶段新缝

```python
class PayloadUpdater:
    def install(self, *, package: Path, trust_store: Path) -> UpdateReceipt: ...
    def complete(self, *, success: bool) -> RuntimeSelection: ...
    def rollback(self) -> RuntimeSelection: ...
    def resolve_active(self) -> RuntimeSelection: ...
```

- 增量包只允许 `app/` 与 `frontend/dist/`;完整包内的 `updater/` 本身不随 payload 更新。
- release 系统对 canonical `manifest.json` 做 Ed25519 detached signature;私钥、生产 URL
  与 trust store 不进仓库。运行侧先按 owner-provisioned trust store 验签,再逐文件校验
  size + sha256,拒绝重复路径、越界路径、symlink、未列入 manifest 的文件与压缩炸弹。
- 完整包写入 `runtime/update-compatibility.json`;Python/uv/PostgreSQL artifact、冻结依赖、
  Tauri、bundle scripts、迁移、打包器或 updater 任一指纹不一致时必须拒绝增量并要求整包。
  第一阶段不自动执行 schema migration,迁移指纹变化同样走整包。
- payload 解到 `DATAOPS_STATE_ROOT/updates/versions/`,同文件系统原子 rename 后只原子替换
  active pointer。`complete(success=False)` 必须恢复前一 pointer;健康通过后只保留一份可消费
  rollback pointer。`home/`、PG data、bootstrap secrets 与 venv 均不进入 payload、不参与切换。
- Linux `.deb` 的 `/usr/lib` 资源保持只读;active payload 一律位于用户可写 state root。
  Windows 默认 state root 仍为 bundle root,也可显式外置。
- 在线检查端点、Tauri UI/健康探测编排、trust-store 公钥投放、Tauri 壳自更新与迁移升级
  不在第一阶段接口内;这些决策未定前不得内联 URL、公钥或私钥。

---

## 4. 红线(全部有 CI 检查,违反即拦)

| # | 红线 | CI 检查方式 |
|---|---|---|
| R1 | `services/` 禁止 import 数据库驱动(psycopg/oracledb/pymysql/dmPython/ibm_db) | AST/grep:驱动 import 只允许 `dbclients/` + `infrastructure/` |
| R2 | 业务代码禁止持有明文密码;只能持 SecretRef | grep `ds["password"]` / `ds.get("password")` / `.password =` 在 services 出现即 fail |
| R3 | secret 加密原语限定目录:Fernet/bcrypt 仅 `secretstore/`;`cryptography.hazmat` 仅 `secretstore/`+`license/`;PyNaCl 仅 `license/`。非 secret 指纹用途的 hashlib/hmac(sql_hash / prompt_hash / JWT HMAC)不受限(与 ruff banned-api 配置一致;此前条文写"hashlib 只允许在 secretstore/"与 CI 及存量实践不符,2026-06-11 修正) | ruff banned-api(TID251)import 越界检测 |
| R4 | PG 连接密码禁止写入 secret_refs 表 | 代码审查 + 测试:store_secret 不接受 PG_*_PASSWORD kind |
| R5 | 日志禁止出现敏感值 | structlog 脱敏 processor(强制)+ 测试:打印含 password 的 dict 输出必须 REDACTED |
| R6 | ResultSet 禁止持有 DbCursor | grep `ResultSet` 类定义不得有 cursor 字段;cursor 持有时长 ≤ cursor_max_hold_seconds |
| R7 | Workflow 节点必须在白名单内(**2.4.0 Workflow 域生效;R7 单测已在 CI 激活**,见 `tests/unit/test_redlines.py`;设计见 ADR-0009) | 校验 job_kind ∈ ALLOWED_WORKFLOW_NODE_KINDS;禁 shell/python/任意HTTP |
| R8 | 配置文件禁止明文密码字段 | gitleaks + `config/*.json|yml` 出现明文 `password:` fail |
| R9 | CI 不跑 SQLite backend(2.0 不支持) | 测试矩阵只有 PG |
| R10 | f-string 拼接 `uuid4().hex` / `str(uuid4())` 禁止(拼前缀让 id 长度不可预测,超 schema 字段) | ast-grep:`tools/lint/rules/r10_no_uuid_prefix_concat.yml`(触发案例见 `docs/agent-playbook.md §2`) |

**ALLOWED_WORKFLOW_NODE_KINDS**(R7):
```python
{"sql_query","sql_explain","compare_run","scenario_materialize",
 "scenario_run_all","lineage_analyze","export_excel","notify","sleep","branch"}
```

2.4.x 实际开放其中 8 种:`sql_query/sql_explain/compare_run/lineage_analyze/
export_excel/notify/sleep/branch`。Scenario 两种仍是白名单内但暂不支持,必须返回
`unsupported_node_kind`,不得与白名单外的 `forbidden_node_kind` 混淆。

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
- Compare → 2.2 / Lineage → 2.3 / Workflow → 2.4 / Scenario → 2.6(2026-06-19 roadmap 重排,与 AI Copilot 一体)
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
