# DataOpsStudio 1.x AS-IS 现状文档

> **范围**: 基于 2026-05-28 HEAD `ec9abe6` 实际代码扫描产出。
> **用途**: 给 2.0 重写参考 —— 移植 DB adapter / 迁移历史数据 / 复用业务规则。
> **约定**: 只描述"现在是什么样",不评价,不提改进。未在代码中读到的标 **未确认**。

---

## 1. 目录结构与模块划分

### 顶层(2 层)

```
DataOpsStudio/
├── main.py                  # FastAPI app 入口 + middleware 装配 + lifespan
├── app/                     # 后端 Python 包
│   ├── ai/                  # LLM provider 抽象 + prompts (enrichment / inference / 错误翻译 / 列映射)
│   ├── api/                 # 23 个 routes 子模块 + 中间件 + 错误处理 + v1 别名安装
│   ├── compare/             # 对比引擎(engine.py)+ 结果落盘抽象(result_writer.py)
│   ├── config_loader.py     # config.yml → os.environ 注入(必须最早 import)
│   ├── dbclients/           # DB 连接 / 池 / 方言(MySQL/Oracle/DM/DB2)
│   ├── lineage/             # SQL 血缘解析(基于 sqlglot,19 个子模块)
│   ├── models/              # Pydantic schema(8 文件)
│   ├── readers/             # SqlReader / ExcelReader / CsvReader / ParquetReader / ExcelFormReader
│   ├── scenarios/           # AI 测试沙盒 DSL + generator + materializer + verifier
│   ├── services/            # 业务服务层(57 文件)— 详见后续章节
│   ├── sqlide/              # SQL 工作台:executor / format / explain / metadata_cache / limit_injector
│   ├── utils/               # 通用工具(sql_guard / paths / logging_config / threading_ctx)
│   └── workflow/            # 作业流 5 类节点 runner + registry
├── frontend/frontend/       # Vue 3 SPA(双层嵌套)
├── config/                  # 持久化 JSON 配置 + scenarios/ + asset_aspects.yml
├── data/                    # SQLite 数据库(audit / jobs / asset_aspects / refresh_tokens 等)
├── results/                 # compare run 结果(parquet 目录 + xlsx + workflow_runs/ + sql_exports/)
├── logs/                    # audit.jsonl + app.log + ai_usage.jsonl
├── docs/                    # 设计文档(COMPARE_RESULT_STORAGE / STREAMING_COMPARE / PARAMETERS 等)
├── tests/                   # 116 个 test_*.py + 4 个 e2e/(Playwright)
├── init_db/                 # docker-compose demo MySQL 初始化 SQL
├── scripts/                 # 部署 / 离线打包 / 日志诊断脚本
├── static/spa/              # frontend build 产物(由 FastAPI 在 /static/spa/ 服务)
├── templates/               # Jinja2 模板(用途未确认)
├── samples/                 # 样例数据文件
├── Dockerfile               # 多阶段构建(node + python:3.12-slim)
├── docker-compose.yml       # app + 可选 demo-db (MySQL 8)
├── requirements.txt         # pip 锁定文件
└── CLAUDE.md                # 项目设计文档(很全)
```

### `app/api/` 子模块(23 个)

`routes.py` 把以下子模块的 router include 起来:

```
system / auth / mfa / projects / datasources / tasks / runs / scheduler
workflows / workflow_runs / history / lineage / lineage_graph / uploads
config_io / ai_utils / search / assets / scenarios / slow_sql
sql_templates / sql_workbench
```

加 3 个内部模块:`routes.py`(聚合)/ `_authz.py`(项目级授权)/ `_error_handler.py`(请求 ID + 错误 envelope)/ `_metrics_middleware.py` / `_shared.py` / `_versioning.py`(`/api/v1/` 别名)。

### `app/services/` 子模块(57 个,只列分类)

- **存储抽象**: `json_store` / `repositories` / `sqlite_store`
- **认证 / 权限**: `auth` / `mfa` / `audit` / `secret_crypto` / `download_token` / `refresh`
- **DB / 元数据**: `datasource_introspect` / `schema_metadata` / `schema_introspection` / `schema_service`
- **执行 / 任务**: `runner`(compare) / `jobs`(async task) / `workflow_engine` / `workflow_nodes` / `scheduler` / `sensors`
- **资源守卫**: `resource_guard` / `memory_guard` / `rate_limit` / `operation_policy` / `query_concurrency`
- **对比辅助**: `compare_schema` / `run_index` / `run_result` / `trace_compare`
- **导出**: `excel_export` / `excel_uploads` / `sql_export` / `file_uploads` / `exporter` / `history_exporter` / `lineage_exporter`
- **历史**: `history` / `workflow_history` / `plan_history`
- **SQL 工具**: `sql_tools` / `sql_preflight` / `sql_stats`
- **慢 SQL**: `slow_sql` / `slow_sql_enhance`
- **血缘**: `lineage_service` / `lineage_ai` / `lineage_ai_config` / `lineage_ai_inference` / `lineage_graph_query` / `lineage_index` / `lineage_stress`
- **资产 / 搜索**: `assets` / `asset_aspects` / `search`
- **观测 / 通知**: `metrics` / `notifier` / `openlineage_emitter`

---

## 2. 数据库 adapter 现状 ★

### 2.1 支持的数据库

`app/models/common.py:DatabaseType` 枚举(`str, Enum`)

| 枚举值 | 字面值 |
|---|---|
| `DatabaseType.MYSQL` | `"MySQL"` |
| `DatabaseType.ORACLE` | `"Oracle"` |
| `DatabaseType.DM` | `"DM"` |
| `DatabaseType.DB2` | `"DB2"` |

**没有** PostgreSQL / SQL Server / SQLite-as-source / Hive 等。

### 2.2 驱动映射

`app/dbclients/drivers.py:DRIVER_MODULES`

| DatabaseType | 候选驱动模块(顺序优先) |
|---|---|
| MYSQL | `pymysql`, `MySQLdb` |
| ORACLE | `oracledb`, `cx_Oracle` |
| DM | `dmPython` |
| DB2 | `ibm_db_dbi`, `ibm_db` |

实际安装(`requirements.txt`): `dmPython` + `pymysql` + `cryptography`。`oracledb` / `ibm_db` 注释掉,需手动装。

**Windows native DLL search path 自动注入**:
- DM: `add_dm_dll_directories()` 扫 `sys.path` 找 `dmpython.libs/` / `dmPython.libs/` / `dmPython/lib|bin|dll` + `DM_HOME` / `DM_DLL_DIR` 环境变量
- DB2: `add_db2_dll_directories()` 扫 `IBM_DB_HOME` / `DB2CLI_HOME` + `~/AppData/Local/Programs/Python/clidriver/{bin,bin/amd64.VC14.CRT,...}`
- 两者都 **幂等**(同路径不重复 set,防 PATH 溢出)

### 2.3 方言抽象 `app/dbclients/dialects/`

ABC `Dialect`(`base.py`)+ registry(`__init__.py`)+ 4 个子类:

```
base.py     - Dialect ABC + CONNECT_TIMEOUT_SECONDS=10 + QUERY_TIMEOUT_SECONDS=300
mysql.py    - MysqlDialect(Dialect)
oracle.py   - OracleDialect(Dialect)
dm.py       - DmDialect(OracleDialect)   # 继承 Oracle,override introspect + connect
db2.py      - Db2Dialect(Dialect)
```

`get_dialect(db_type)` 是唯一入口(lazy import + `_LOADED` flag 防只 import 单方言时 registry 漏注册其它)。

#### Dialect ABC 暴露的能力

| 方法 | 抽象? | 默认实现 | 用途 |
|---|---|---|---|
| `introspect_columns_sql(schema, table)` | 必须 | — | 查表字段(name/data_type/nullable/comment/ordinal) |
| `bulk_columns_sql(schema)` | 否 | `None` | 一次拉一个 schema 全表字段 |
| `connection_test_sql()` | 必须 | — | 连接探活(列别名必须 `ok`) |
| `connect(source, module_name)` | 必须 | — | 建 DB-API connection |
| `statement_timeout_sql(seconds)` | 否 | `None` | 会话超时 SQL(MySQL 用) |
| `apply_call_timeout(conn, seconds)` | 否 | `False` | 连接属性超时(Oracle/DM 用) |
| `estimate_rows_from_explain(conn, sql)` | 否 | `None` | EXPLAIN 估算行数 |
| `list_indexes_sql(schema, table)` | 否 | `None` | 索引列表 |
| `list_views_sql(schema)` | 否 | `None` | 视图列表 |
| `show_create_table_sql(schema, table)` | 否 | `None` | 建表 DDL |

### 2.4 各方言能力矩阵

| 能力 | MySQL | Oracle | DM | DB2 |
|---|---|---|---|---|
| `connection_test` | `select 1 as ok` | `select 1 as ok from dual` | (继承 Oracle) | `select 1 as ok from sysibm.sysdummy1` |
| 语句超时 | `SET SESSION MAX_EXECUTION_TIME` (ms) | `conn.callTimeout` (ms) 属性 | (继承 Oracle) `conn.callTimeout` | `ibm_db.set_option(SQL_ATTR_QUERY_TIMEOUT, sec)` |
| EXPLAIN 行数估算 | `EXPLAIN <sql>`,取 plan `rows` 列 max | `EXPLAIN PLAN SET STATEMENT_ID FOR ...` + `SELECT MAX(cardinality)` + DELETE 清理 | (继承 Oracle) | `EXPLAIN PLAN FOR ...` + `SELECT MAX(STREAM_COUNT) FROM EXPLAIN_STREAM` |
| 列元数据来源 | `information_schema.COLUMNS` | `all_tab_columns LEFT JOIN all_col_comments` | `all_tab_columns`(无 join,comment 返 `''`) | `SYSIBM.SYSCOLUMNS` |
| `bulk_columns_sql` | ✓ | ✓ | ✓ | ✓ |
| 索引列表 | `information_schema.STATISTICS` | `all_indexes JOIN all_ind_columns` | (继承 Oracle) | `None` |
| 视图列表 | `information_schema.VIEWS` | `all_views` / `user_views` | (继承 Oracle) | `None` |
| `show_create_table` | `SHOW CREATE TABLE \`s\`.\`t\`` | `None`(DBMS_METADATA.GET_DDL 复杂) | (继承 Oracle) | `None` |
| 标识符大小写 | 反引号包裹,原样保留 | `UPPER('schema')` + `UPPER('table')` 包裹查询 | (继承 Oracle) | `UPPER('schema')` + `UPPER('table')` |
| 标识符 quote | `` `name` `` | `"name"` | `"name"`(继承 Oracle) | `"name"`(默认 SQL92) |
| `nullable` 编码 | `'YES'`/`'NO'`(`IS_NULLABLE`) | `'Y'`/`'N'`(`NULLABLE`) | (继承 Oracle) | `'Y'`/`'N'`(`NULLS`) |
| 连接 dsn 形式 | `host=`/`port=`/`user=`/`password=`/`database=`/`charset=` 等 kwargs | `dsn = host:port/service` 或 `extra.dsn` | `server=`/`port=`/`user=`/`password=`/`schema=` + 老接口 positional 兜底 | conn string: `DATABASE=...;HOSTNAME=...;PORT=...;PROTOCOL=TCPIP;UID=...;PWD=...;CONNECTTIMEOUT=...;` |

#### 标识符校验白名单(`services/datasource_introspect.py:_validate_identifier`)

允许字符: 字母 / 数字 / `_` / `$` / `.` / `-`。其它(空白 / 引号 / 分号 / 反斜杠 / 括号)抛 `ValueError`。schema 名含 `-` 被显式允许(MySQL `live-monitor` 等)。

### 2.5 SQL 安全 `app/utils/sql_guard.py`

`validate_readonly_sql(sql)`:
- 必须首词 `SELECT` 或 `WITH`(注释先 strip)
- 拒绝多语句(以 `;` 切割后第二段非空 → 拒)
- 拒绝 `FOR UPDATE`(正则匹配)
- 禁词集合(token-level 匹配):
  `alter / call / create / delete / drop / execute / grant / insert / lock / merge / replace / revoke / truncate / update`

### 2.6 连接池 `app/dbclients/pool.py`

- LIFO 栈 + lock + idle TTL
- `max_size` 默认 **4**(可由 `source.extra.pool_max_size` 覆盖)
- `idle_seconds` 默认 **600**(可由 `source.extra.pool_idle_seconds` 覆盖)
- 每 `datasource_id` 一个池(`_pools: dict[str, tuple[fingerprint, _DatasourcePool]]`)
- `fingerprint` 改变(host/port/database/username 变了)时 → 旧池失效重建
- `source.extra.disable_pool=true` → 池失效
- MySQL 路径 acquire 时 `_ping_mysql(conn)` 跑 `SELECT 1` 验活,失败丢弃
- `borrow(source, connect_factory)` 是 context manager,异常时 `_safe_close(conn)` 不归还池

### 2.7 流式读 / 大查询 `app/dbclients/factory.py`

`stream_rows(source, sql, *, max_rows, chunk_size=1000)`:
- **MySQL + pymysql** → `SSCursor`(server-side unbuffered cursor),边拉边发
- **其它驱动** → 普通 cursor + `fetchmany(chunk_size)`(driver-level batch buffer 小)
- SSCursor 期间该连接不能跑其它 SQL
- 失败时 fallback 普通 cursor(代价 OOM 风险,有 max_rows 兜底)

### 2.8 取消查询

**没有 driver-level cancel**。机制是:
1. `JobInfo.cancel_requested` flag(in-memory)
2. Worker 线程在每个安全点(streaming 拉一批 / engine 处理一批)轮询 `cancel_check()`
3. 命中即抛 `JobCancelled` 退出
4. 数据库 cursor 由 finally 块 `close()` 释放(中间 cancel 不主动 kill server-side 查询;靠 statement timeout 兜底)

Statement timeout 两路径(`factory._apply_statement_timeout`):
- **路径 A**(连接属性): `dialect.apply_call_timeout(conn, sec)` —— Oracle/DM 走 `conn.callTimeout`,DB2 走 `ibm_db.set_option`
- **路径 B**(会话 SQL): `dialect.statement_timeout_sql(sec)` —— MySQL `SET SESSION MAX_EXECUTION_TIME`
- 优先试路径 A,失败 / 不支持 fallback B,B 也失败只记 warning(不影响真查询)

默认值 `env DATAOPS_DB_STATEMENT_TIMEOUT_SECONDS`(默认 900s),`<= 0` 关闭。`ContextVar _query_timeout_override` 给单任务覆盖(`runner.run_task` 入口 push `task.limits.query_timeout_seconds`)。

### 2.9 涉及的代码文件清单

```
app/dbclients/factory.py          580 行  连接 / fetch_rows / stream_rows / fetch_column_details / iter_rows / timeout
app/dbclients/pool.py             203 行  LIFO 池 + TTL + MySQL ping + 指纹失效
app/dbclients/drivers.py          ~150 行 DRIVER_MODULES + Windows DLL search 注入
app/dbclients/dialects/base.py    ~120 行 Dialect ABC + 共享常量
app/dbclients/dialects/mysql.py   ~170 行
app/dbclients/dialects/oracle.py  ~190 行
app/dbclients/dialects/dm.py      ~80 行  继承 Oracle,override 4 个方法
app/dbclients/dialects/db2.py     ~150 行
app/services/datasource_introspect.py  405 行  introspect_columns/indexes/row_count + 标识符白名单
app/utils/sql_guard.py            121 行
```

---

## 3. 数据存储结构 ★

### 3.1 JSON 配置文件(`config/`)

路径常量在 `app/utils/paths.py`。运行时由 `JsonStore[ModelT, CreateT]`(`app/services/json_store.py`)管理 — 基于 mtime 缓存失效的线程安全泛型封装,每个 store 模块级单例在 `app/services/repositories.py`:

```python
datasource_store        = JsonStore[DataSource, DataSourceCreate](DATASOURCES_FILE, DataSource)
task_store              = JsonStore[CompareTask, CompareTaskCreate](TASKS_FILE, CompareTask)
workflow_store          = JsonStore[Workflow, WorkflowCreate](WORKFLOWS_FILE, Workflow)
workflow_template_store = JsonStore[WorkflowTemplate, WorkflowTemplateCreate](WORKFLOW_TEMPLATES_FILE, WorkflowTemplate)
```

`users.json` / `projects.json` / `lineage_ai.json` / `sql_templates.json` 由各自 service 模块直接管(不走 JsonStore 泛型)。

| 文件 | 内容 | Pydantic 模型 |
|---|---|---|
| `config/datasources.json` | list[DataSource] | `app/models/datasource.py` |
| `config/tasks.json` | list[CompareTask] | `app/models/compare.py` |
| `config/workflows.json` | list[Workflow] | `app/models/workflow.py` |
| `config/workflow_templates.json` | list[WorkflowTemplate] | `app/models/workflow.py` |
| `config/users.json` | list[User] | `app/models/user.py:User` |
| `config/projects.json` | list[Project] | `app/models/user.py:Project` |
| `config/lineage_ai.json` | dict(AI provider 配置) | 无 model,纯 dict |
| `config/jobs.json` | dict[job_id, JobInfo](legacy,已切到 SQLite) | `app/services/jobs.py` 内部 dict |
| `config/sql_templates.json` | list[SqlTemplate] | `app/models/sql_template.py` |
| `config/scenarios/*.yml` | Scenario DSL | `app/scenarios/models.py:Scenario` |
| `config/asset_aspects.yml` / `.example.yml` | aspect 类型 schema(6 内置) | 纯 YAML |
| `config/lineage_group_rules.yml` / `.example.yml` | 业务分组规则 | YAML |
| `config/config.yml.example` | env var 注入(JWT_SECRET 等) | YAML |
| `config/.dataops_secret.key` | Fernet 加密本地 key(自动生成) | bytes,权限 0600 |
| `config/metadata_cache/*.json` | sqlide 元数据缓存(per-ds) | dict |

#### 3.1.1 `DataSource` 字段

```python
class DataSource(_AllowFlagsMixin):
    id: str                      # min_length=1
    name: str                    # min_length=1
    db_type: DatabaseType        # MySQL/Oracle/DM/DB2
    host: str                    # min_length=1
    port: int                    # gt=0, le=65535
    database: str = ""
    username: str = ""
    password: str = ""           # ★ 明文(JsonStore 落盘 0600;API 出去脱敏)
    extra: dict[str, Any]        # charset / dsn / conn_str / pool_max_size 等
    project_id: str = ""         # 空 = 全局可见
    environment: Literal["unknown","sandbox","staging","prod"] = "unknown"
    environment_verified: bool = False
    # 8 个 allow_* 标志, 默认全 False 除 allow_select=True:
    allow_select / allow_explain / allow_dm_explain / allow_oracle_plan_table
    allow_schema_import / allow_schema_save / allow_scenario_write / allow_record_task
```

策略矩阵在 `app/services/operation_policy.py:assert_operation_allowed`。

#### 3.1.2 `CompareTask` 字段(`app/models/compare.py`)

字段(摘自 `tasks.example.json` + 模型):
- `id`, `name`
- `source_id`, `target_id`(datasource id 引用)
- `sql_mode`: `"single"` / `"double"`
- `source_kind`/`target_kind`: `sql` / `excel` / `csv` / `parquet` / `excel_form`(`SourceKind` 枚举)
- SQL 端: `source_sql`, `target_sql`
- Excel 端: `source_excel_path`, `source_sheet`, `source_header_row`, `target_excel_path`, `target_sheet`, `target_header_row`
- CSV/Parquet 端: `source_file_path`, `target_file_path`, `source_file_encoding`, `target_file_encoding`, `source_csv_delimiter`, `target_csv_delimiter`
- `key_columns: list[str]`
- `rules: CompareRules`(`ignore_columns` / `column_mappings` / `numeric_tolerance` / `trim_strings` / `case_insensitive` / `empty_as_null` / `schema_policy` 等)
- `limits: RunLimits`(`max_rows` / `export_max_rows` / `fetch_chunk_size` / `stream_compare` / `result_format` / `persist_same_bucket` / `query_timeout_seconds` / `run_disk_quota_mb` 等)
- `project_id`

#### 3.1.3 `User` 字段(`app/models/user.py`)

```python
class User(BaseModel):
    id: str
    username: str                            # max_length=64
    password_hash: str                       # ★ bcrypt hash(永不返明文)
    role: Literal["admin","editor","viewer"]
    display_name: str = ""
    created_at: str = ""
    mfa_secret_encrypted: str = ""           # ★ secret_crypto 加密(Fernet)的 TOTP secret
    mfa_enabled: bool = False
    mfa_recovery_codes_hashed: list[str]     # ★ bcrypt hash 后的 recovery codes(一次性消费即从列表删)
```

#### 3.1.4 `Project` 字段

```python
class Project(BaseModel):
    id: str
    name: str            # max_length=64
    description: str = ""
    owner_id: str = ""   # User.id
    members: list[str]   # User.id list(含 owner)
    created_at: str = ""
```

#### 3.1.5 `lineage_ai.json` 字段(`app/services/lineage_ai_config.py`)

```
{
  "provider": "mock"/"openai_compatible"/"anthropic"/"ollama"/"off",
  "model": "...",
  "api_key_encrypted": "fernet:..."   # ★ Fernet 加密(可空字符串)
  "base_url": "...",
  "enable_inference": bool,
  "enable_auto_translation": bool,
  ...
}
```

`api_key` 由 `save_lineage_ai_config` 自动 encrypt;读取走 `decrypt_secret`。env `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 作 fallback(`api_key_source` 字段标 `stored` / `env`)。

### 3.2 SQLite(`data/dataops.db`)

`app/services/sqlite_store.py` 集中所有 `CREATE TABLE IF NOT EXISTS`。启动时 `_init_schema()` 跑一次。

#### 3.2.1 表清单(9 张)

| 表名 | 主键 | 用途 |
|---|---|---|
| `audit_logs` | `id INTEGER AUTOINCREMENT` | mutating endpoint 审计流水(从 `logs/audit.jsonl` 迁来,启动 `migrate_audit_jsonl`) |
| `jobs` | `id TEXT` | 异步任务状态(从 `config/jobs.json` 迁来,启动 `migrate_jobs_json`) |
| `asset_aspects` | `id INTEGER AUTOINCREMENT` | 资产 classification(owner/pii/sla/sensitive/tag/business_term) |
| `asset_aspect_history` | `id INTEGER AUTOINCREMENT` | aspect 变更轨迹(append-only,insert/update/delete) |
| `revoked_tokens` | `jti TEXT` | JWT 吊销表(logout / 重放检出) |
| `refresh_tokens` | `jti TEXT` | refresh rotation(replaced_by / revoked_at) |
| `download_nonces` | `jti TEXT` | 下载 token 一次性消费 |
| `slow_sql_plans` | `id INTEGER AUTOINCREMENT` | slow-sql plan 历史(plan-diff 用) |
| `run_index` | `run_id TEXT` | 所有 compare run 的统一持久化(替代扫文件系统) |

#### 3.2.2 关键表字段(精简,详见 `_init_schema`)

**`audit_logs`**: `ts / user_id / username / method / path / status / request_id / resource / resource_id / project_id / extra (JSON blob)`,索引 `(ts DESC)` / `(user_id, ts)` / `(request_id)`

**`jobs`**: `kind(compare_task/workflow_run) / status / task_id / workflow_id / run_id / started_at / finished_at / cancel_requested(INT) / payload(JSON dict 完整序列化)`

**`asset_aspects`**: `asset_kind / asset_name / aspect_type / value(JSON) / project_id / updated_at / updated_by`,unique `(asset_kind, asset_name, aspect_type, project_id)`

**`refresh_tokens`**: `user_id / exp / issued_at / replaced_by(rotation 后的新 jti) / revoked_at`

**`run_index`**: `run_id / job_id / task_id / workflow_run_id / project_id / owner_user_id / source_ds_id / target_ds_id / status (reserved/running/success/failed/cancelled/aborted_guard/deleted) / requested_at / started_at / finished_at / result_format / stream_compare(INT) / max_rows / estimated_bytes / disk_bytes / peak_rss_mb / guard_reason / result_path / error`

**`slow_sql_plans`**: `ts / datasource_id / dialect / sql_text / sql_hash(SHA-256 归一化后) / scenario_id / workload_name / plan_json / issues_json / suggestions_json`

### 3.3 结果文件(`results/`)

#### 3.3.1 Parquet 目录格式(`results/<run_id>/`,文件 `app/compare/result_writer.py:ParquetResultWriter`)

```
results/
  <run_id>/
    meta.json             # summary + 文件清单 + schema + run params + same.sample
    only_source.parquet   # 全量
    only_target.parquet   # 全量
    diff.parquet          # 全量(含 changes 嵌套 struct)
    same.parquet          # 仅当 limits.persist_same_bucket=True
    sample.json           # 头 N 行抽样(未确认是否所有 run 都生成)
    export.xlsx           # 按需异步生成
```

`meta.json` 形态(`format_version=2`,详见 `docs/COMPARE_RESULT_STORAGE.md`):
```json
{
  "run_id": "...",
  "task_id": "...",
  "task_name": "...",
  "started_at": "...",
  "elapsed_seconds": 12.34,
  "source_rows": 1234567,
  "target_rows": 1234600,
  "summary": {"only_source": 12, "only_target": 45, "diff": 678, "same": 1233832},
  "buckets": [
    {"name":"only_source", "path":"only_source.parquet", "rows":12, "bytes":4096, "mode":"full"},
    ...
    {"name":"same", "path":null, "rows":1233832, "bytes":0, "mode":"count_only", "sample":[...]}
  ],
  "schema": {"key_columns":["id"], "source_columns":[...], "target_columns":[...]},
  "format_version": 2,
  "engine_version": "..."
}
```

`bucket.mode`: `"full"` / `"count_only"`(只 count + sample,默认 same 桶)/ `"sample_only"`(未启用)。

Parquet 压缩: `snappy` 默认 / `zstd`(`> 100MB` 切换)。`batch_size=10_000` 行 flush 一次 row group。

#### 3.3.2 旧 JSON 格式(`results/<run_id>.json`)

`JsonResultWriter`(`result_writer.py`)— 单文件 JSON,4 桶 rows 全在内存里序列化。`format_version=1`。读侧 detect:目录形态走 parquet,单文件 `.json` 走 legacy。

#### 3.3.3 其它结果产物

- `results/workflow_runs/<run_id>.json` — WorkflowRun 落盘(`services/workflow_history.py`)
- `results/sql_exports/` — SQL 工作台异步导出(CSV / Excel / JSON / SQL)
- `results/<run_id>.xlsx` — compare 异步导出(用 `submit_excel_export`)
- lineage_script workload 走 `results/<run_id>.json` 格式(`run_id` 形如 `lineage_script_<YYYYMMDDHHMMSS>_<8hex>`)

### 3.4 日志(`logs/`)

| 文件 | 内容 |
|---|---|
| `logs/audit.jsonl` | append-only,每行一 JSON;启动时迁移到 SQLite 的 `audit_logs`,保留文件做回滚 |
| `logs/app.log` | 应用日志(`utils/logging_config`,`DATAOPS_LOG_FORMAT=json` 切结构化) |
| `logs/ai_usage.jsonl` | AI provider 调用流水(`app/ai/usage_log.log_call`) |

### 3.5 敏感数据存储总结

| 数据 | 存哪 | 字段名 | 形态 |
|---|---|---|---|
| 用户登录密码 | `config/users.json` | `User.password_hash` | bcrypt hash(`bcrypt>=4.0`) |
| MFA TOTP secret | `config/users.json` | `User.mfa_secret_encrypted` | Fernet 加密(secret_crypto) |
| MFA recovery codes | `config/users.json` | `User.mfa_recovery_codes_hashed: list[str]` | bcrypt hash 后存,一次性消费即删 |
| 数据源密码 | `config/datasources.json` | `DataSource.password` | **★ 明文**,文件权限 0600,API 响应脱敏 |
| AI provider API key | `config/lineage_ai.json` | `api_key_encrypted` | Fernet 加密(前缀 `fernet:`) |
| Fernet 主密钥来源 | env `DATAOPS_CONFIG_SECRET` / `DATAOPS_SECRET_KEY` → 否则读 `config/.dataops_secret.key`(自动生成,权限 0600) | — | `Fernet.generate_key()` |
| JWT secret | env `DATAOPS_JWT_SECRET`(未设有 dev fallback,启动 warning) | — | 字符串 |
| JWT access token | 内存 + `Authorization: Bearer` header | — | HS256 签名 |
| JWT refresh token | SQLite `refresh_tokens.jti` + 客户端 localStorage | `User.id` 关联 | rotation 时老 jti 标 `replaced_by=新jti` |
| revoked JWT jti | SQLite `revoked_tokens` | `jti / exp / revoked_at / user_id` | exp 后被 prune |

### 3.6 Fernet 加密细节(`app/services/secret_crypto.py`)

- 加密前缀: `"fernet:"`(`PREFIX` 常量)
- 算法: `cryptography.fernet.Fernet`(AES-128-CBC + HMAC-SHA256)
- key 来源优先级: env `DATAOPS_CONFIG_SECRET` → env `DATAOPS_SECRET_KEY` → 本地文件 `config/.dataops_secret.key`
- env 形态: 任意字符串,内部走 `base64.urlsafe_b64encode(sha256(secret).digest())` 生成 Fernet key
- 本地文件: 第一次 `Fernet.generate_key()` 写入,文件权限设为 `0600`
- 老明文(没 `fernet:` 前缀)读出来按原样返回;下次保存自动加密

---

## 4. 核心业务规则所在位置

### 4.1 数据对比(Compare)

| 关注点 | 位置 |
|---|---|
| 引擎(归桶 only_source/only_target/diff/same) | `app/compare/engine.py:compare_rows` / `compare_rows_streaming` / `compare_sorted_row_events` / `compare_sorted_row_iterators` |
| 行键提取 | `engine.py:_row_key / _row_changes` |
| 数值/字符串归一化(trim_strings / case_insensitive / empty_as_null / numeric_tolerance / 日期 ISO 化) | `engine.py:_normalize_value / _normalize_key_value / _values_equal / _decimal_or_none / _temporal_to_iso` |
| 字段映射(column_mappings) | `engine.py:_rules_with_positional_mappings / _target_column_name / _mapped_column_name / _resolve_column / _normalize_column_name` |
| 流式/排序输入归并 | `engine.py:_ensure_sorted_key / compare_sorted_row_iterators` |
| 入口编排(从 Task → reader → engine → writer → meta + 落盘 + run_index + 通知) | `app/services/runner.py:run_task`(50)/ `_run_task_inner`(252)/ `build_reader`(536)/ `_maybe_promote_large_task`(181)/ `_check_mid_run_disk`(495)/ `_cleanup_partial_parquet`(518) |
| 结果落盘抽象 | `app/compare/result_writer.py:ResultWriter`(Protocol) / `JsonResultWriter` / `ParquetResultWriter` / `feed_buckets` |
| Schema 报告 / 列差异警告 | `app/services/compare_schema.py:build_schema_report / column_warnings / uniquify_columns` |
| 跨源 row reader 抽象 | `app/readers/{base.py(RowReader), sql_reader, excel_reader, csv_reader, parquet_reader, excel_form_reader}.py` |
| 列 schema policy(warn / strict) | `models/compare.py:CompareRules.schema_policy` + `services/runner.py` 校验路径 |

CompareRules / RunLimits 字段定义在 `app/models/compare.py`(详见 §3.1.2)。

### 4.2 场景(Scenario)— `app/scenarios/`

| 文件 | 用途 |
|---|---|
| `models.py` | Pydantic DSL: `Scenario / TableDef / ColumnDef / IndexDef / ColumnOverride / AnomalyDef / WorkloadDef / DomainDef / AISettings`,`extra='forbid'` 拦笔误 |
| `loader.py` | `load_scenario(path) → Scenario` + `list_scenarios()` 扫 `config/scenarios/*.yml`(坏文件单列错因) |
| `generator.py` | 7 个 column generator(`uuid_short / random_int+zipf / realistic / timestamp / enum+weights / constant / sequence+prefix`)+ 6 个 anomaly kind(`missing_rows / extra_rows / value_drift / null_drift / duplicate_pk / type_mismatch`),seed 决定性 |
| `ai_filler.py` | LLM 给 realistic 列业务样本池 + 表中文描述,走 lineage_ai provider 抽象,`max_calls` cap 50 |
| `dialects/` | materialize 方言(`base.MaterializeDialect` ABC + `mysql.py` + `oracle.py`,DM 复用 Oracle) |
| `materializer.py` | 纯函数 `build_materialize_plan` 产 DDL + INSERT 计划;`apply_plan(SqlExecutor 协议)` 不管事务边界 |
| `runtime.py` | datasource_id → pool.borrow → cursor → apply_plan + commit(IO 边界) |
| `recorder.py` | compare_task workload → `CompareTaskCreate` 走 task_store.create;lineage_script workload → `analyze_sql_lineage` + 写 `results/<run_id>.json` |
| `verifier.py` | actual vs yml expected 回归校验(5 状态:pass/fail/no_expected/no_task/no_run),按命名约定 `<scenario_id> · <workload_name>` 反查 task |
| `orchestrator.py` | `run_all(scenario, datasource_id, *, ai_fill)` 6 步串成一次调用 |
| `templating.py` | `render_template(sql, variables)` `{{name}}` 占位符(切片 15) |
| `faker_providers.py` / `faker_fallback.py` | `realistic` 列在 provider=off 时的 Faker locale-aware fallback(切片 17) |
| `yml_importer.py` | 用途 **未确认**(可能是 schema → yml 反向导入) |

### 4.3 血缘(Lineage)— `app/lineage/`

基于 `sqlglot>=26.0`。19 个子模块按职责拆:

| 文件 | 用途 |
|---|---|
| `analyzer.py` | `analyze_sql_lineage(sql_text, dialect, schema)` 单脚本入口,编排其它模块 |
| `batch_analyzer.py` | `analyze_lineage_batch` 多文件(`.sql`/`.txt`/`.zip`)血缘 |
| `segments.py` | 存储过程分段(BEGIN/END token 平衡,PL/SQL 控制流壳子跳过) |
| `columns.py` | 字段级 lineage 抽取(insert_mappings) |
| `dml.py` | DML 语句解析(INSERT/UPDATE/MERGE/DELETE/CTAS/INSERT OVERWRITE/TRUNCATE) |
| `tables.py` / `roles.py` | 表引用归一化 + role 标签 |
| `aggregation.py` | 按目标表聚合 DML,识别 `delete_insert / truncate_insert / merge / append / mixed` 等 refresh_mode + `collect_procedure_operations` |
| `grouping.py` + `config/lineage_group_rules.yml` | 业务分组规则(schema/basename/title 三类 matcher) |
| `semantic.py` | 把 target_summary / table_roles / procedure_segments / parse_errors / dynamic_sql_segments 收口成 `result["semantic_lineage"]` |
| `preprocess.py` | SQL 解析前归一化(全角标点 / `${name}` 模板变量 / `< =` 比较运算符空白 / Oracle alias 形式 / `DELETE FROM tbl AS alias` / `RETURNING INTO` 剥离等 11 处) |
| `dialects.py` | `_resolve_dialect()` 把 dm/dameng → oracle / ob_mysql/oceanbase → mysql / ob_oracle → oracle |
| `variables.py` | PL/SQL 变量提取(package_constant / package_variable / declare_constant / declare_variable / 模板变量) |
| `clauses.py` / `helpers.py` / `_common.py` / `warnings.py` / `report.py` / `graph.py` | 工具模块 |

输出形态 `result` 主键(从 `analyzer.analyze_sql_lineage` 推断,详见模型 `app/models/lineage.py`):
- `tables` / `dml_statements` / `procedure_segments` / `target_summary` / `table_roles` / `graph_edges` / `graph_groups` / `insert_mappings` / `parse_errors` / `dynamic_sql_segments` / `semantic_lineage` / `variables` / `ai_inferred`(异步填充)

**AI 兜底 3 类**(`app/ai/`,默认关需 admin 开):
- P1 parse_errors
- P2 dynamic_sql
- P3 column_attribution

### 4.4 SQL 工作台 — `app/api/sql_workbench.py` + `app/sqlide/`

| 文件 | 用途 |
|---|---|
| `api/sql_workbench.py` | 14 个 endpoint:consoles CRUD / execute / executions/{id} status+cancel / export / export status+download / format / explain / metadata 等 |
| `sqlide/models.py` | `Console / ConsoleCreate / ConsoleUpdate / ExecuteRequest / ExecuteResponse / HistoryEntry` |
| `sqlide/executor.py` | `execute_sql(...)` 业务核心 — readonly check + limit injection + EXPLAIN 预警 + cell 序列化 + 慢 SQL 落盘 |
| `sqlide/runtime.py` | 独立 `ThreadPoolExecutor(max_workers=4)` + execution registry(in-memory) — 跟 `jobs.py` 的 max_workers=2 池**独立** |
| `sqlide/limit_injector.py` | `inject_limit(sql, max_rows, db_type)` — 自动补 `LIMIT` / `FETCH FIRST n ROWS ONLY`(预览时强制) |
| `sqlide/explain.py` | `explain_sql(source, sql)` — MySQL 直 `EXPLAIN`,其它方言走 dialect |
| `sqlide/format.py` | `format_sql(sql, db_type)` — 基于 sqlglot 格式化 |
| `sqlide/metadata.py` / `metadata_cache.py` | schemas / tables / columns 反向元数据 + per-ds JSON 缓存(scope=schemas/tables/columns/columns-bulk/indexes/views) |
| `sqlide/search.py` | console 内搜索(用途 **未确认**) |
| `services/sql_tools.py` | `sql_assist(sql, dialect, target_dialect)` — output_columns 抽取 / 自动注入别名(`rewrite_sql_inject_aliases`) |
| `services/sql_export.py` | 独立 `ThreadPoolExecutor(max_workers=2)` 异步 SQL 结果导出(CSV/Excel/JSON/SQL),公式注入防御 |
| `services/sql_preflight.py` | EXPLAIN-based row estimate,发现超 `max_rows × 10` 加 warn finding |

### 4.5 慢 SQL 分析 — `app/services/slow_sql.py`(755 行)

`analyze_sql(source, sql, max_plan_rows)` 派发到方言子函数:

| 方言 | 实现函数 | 规则数 |
|---|---|---|
| MySQL | `_analyze_mysql` + `detect_issues` + `build_suggestions` | 4 规则: `full_table_scan` / `filesort` / `using_temporary` / `high_row_scan` |
| Oracle | `_analyze_oracle` + `detect_oracle_issues` + `build_oracle_suggestions` + `_fetch_oracle_plan` | 6 规则: `full_table_scan` / `sort_order_by` / `sort_group_by` / `nested_loops_high_card` / `high_cost` / `high_row_scan` |
| DM | `_analyze_dm` + `detect_dm_issues` + `build_dm_suggestions` + `_extract_dm_text/_card` | 复用 Oracle 路径(PLAN_TABLE 协议一致) |
| DB2 | 通过 `Db2Dialect.estimate_rows_from_explain`(EXPLAIN_STREAM)— slow_sql.py 内**未确认**有专门 DB2 分析路径 |

`enrich_via_ai(sql, plan, issues, suggestions, expected_optimizations, dialect)` — LLM 复核 + 补漏 + 给 DDL/SQL 改写 + 对比 yml `expected_optimizations` 算 coverage_pct。Provider=off 时返 ok=False + 占位字段(200 降级)。

API endpoint: `POST /api/slow-sql/analyze` + `POST /api/slow-sql/enrich`(`app/api/slow_sql.py`)。

---

## 5. 执行模型现状

**共 3 套独立的 ThreadPoolExecutor**(都在 `app/services/` / `app/sqlide/`):

| 池 | 文件 | 大小 | 用途 |
|---|---|---|---|
| `_executor` | `services/jobs.py:29` | `ThreadPoolExecutor(max_workers=2)` | compare task / workflow run / excel_export |
| `_executor` | `services/sql_export.py:106` | `ThreadPoolExecutor(max_workers=2, thread_name_prefix="sql-export-")` | SQL 工作台结果导出 |
| `_executor` | `sqlide/runtime.py:97` | `ThreadPoolExecutor(max_workers=4, thread_name_prefix="sql-workbench-")` | SQL 工作台 execute_sql(独立池避免抢) |

### 5.1 compare / workflow / excel_export — `services/jobs.py`

- `submit_task_run(task_id, *, owner_user_id, project_id, ...)` → `_executor.submit(_run_job, job_id, task_id)`
- `submit_workflow_run(workflow_id, ...)` → `_executor.submit(_run_workflow_job, ...)`
- 状态记 SQLite `jobs` 表 + in-memory dict(`_jobs`)
- `JobInfo`: `id / kind / status (pending/running/succeeded/failed/cancelled/cancelling) / task_id / workflow_id / run_id / cancel_requested / payload / owner_user_id / project_id / target_run_id`
- 取消: `cancel_job(job_id)` 设 `cancel_requested=True`,worker 轮询命中即 `raise JobCancelled`
- TTL: `cleanup_jobs(ttl_seconds)` 周期清理 expired terminal job
- 跨线程 context: `submit_with_context`(`utils/threading_ctx`)透传 request_id ContextVar 到 worker 线程
- 重启后:运行中的 job 标 `failed`(`_load_jobs_from_disk`,**未确认**实现细节)

### 5.2 作业流引擎 — `services/workflow_engine.py`

`run_workflow(workflow, variables, runners, cancel_check, resume_from, from_node_id)`:
- 按 `depends_on` 拓扑序执行(`topological_order`)
- 节点类型 5 种,runner 在 `app/workflow/nodes/<type>.py`,集中注册在 `app/workflow/registry.NODE_RUNNERS`:
  - `params` — 参数节点
  - `compare` — 调 `runner.run_task`
  - `lineage` — 调 `analyze_sql_lineage`
  - `http` — HTTP 调用
  - `excel_export` — 异步 Excel 导出
- 单节点失败 → 下游 `SKIPPED`、旁路继续
- `when:` 表达式条件性跳过
- 局部重跑: `from_node_id` 指定,上游沿用上次 output(`reused=true`),祖先必须上次 SUCCESS
- 变量插值: `${name}` / `${nodes.X.Y}` / `${var | sql_in}` 过滤器(详见 `docs/PARAMETERS.md`)
- WorkflowRun 落盘到 `results/workflow_runs/<run_id>.json`(`services/workflow_history.py`)

### 5.3 调度 — `services/scheduler.py`

- 接 APScheduler `BackgroundScheduler`(timezone="Asia/Shanghai")
- `start_scheduler(interval_seconds)` 在 lifespan 启动
- 每个 `status=active` + `schedule_cron` 非空的 workflow → `CronTrigger`
- sync 任务定时重读 `workflow_store` 增删改 cron job
- APScheduler 不在环境时 fallback 到 polling loop(`tick(now)` 手动 due-check)
- Sensor: `services/sensors.py` 两类
  - `file`: config = `{path, mode: "exists"|"newer_than", check_size}`
  - `workflow_success`: config = `{workflow_id}`,按 `last_seen_run_id` 去重
  - 命中后 `submit_workflow_run(trigger="sensor:<type>")`

### 5.4 SQL 工作台 — `sqlide/runtime.py`

- `execute_sql(...)` 通过 `submit_with_context(_executor, _run)` 异步
- in-memory `Execution` registry(无持久化)
- 取消: `cancel_execution(execution_id)` 设 `cancel_reason`(实际中止靠 statement timeout)

### 5.5 SQL 工作台导出 — `services/sql_export.py`

- 独立 `_executor`(max_workers=2)
- in-memory Export registry
- 导出格式: CSV / Excel / JSON / SQL(INSERT 语句),公式注入防御(`=`/`+`/`-`/`@` 开头加 `'` 前缀)

### 5.6 Compare excel_export — `services/excel_export.py`

- 复用 `jobs.py:_executor`(max_workers=2)
- `JobInfo.kind=excel_export` 区分
- `submit_excel_export(run_id)` 提交,前端 poll 后下载

---

## 6. 测试现状

### 6.1 后端 `tests/`

- **总文件数**: 116 个 `test_*.py`(`tests/` 直接子目录;另含 `tests/e2e/` 4 个 Playwright 测试 + `conftest.py` 等共享 fixture)
- **总 `def test_` 数**: **2345 个**(grep 出来,可能含 helper / nested test)
- CLAUDE.md 自述基线 **1589 passed / 0 failed / 1 skipped**(本地 pytest 全量,排除 2 个慢 lineage 集)

### 6.2 按文件前缀分类(top 12)

| 前缀 | 文件数 | 大致覆盖 |
|---|---|---|
| `test_sql_*` | 15 | SQL 工作台 / preflight / stats / tools / format / limit_injector / templates |
| `test_scenarios_*` | 13 | scenario DSL / generator / materializer / verifier / orchestrator / templating / ai_filler / dialects |
| `test_lineage_*` | 12 | analyzer / batch / columns / dml / preprocess / semantic / variables / dialects / aspects / Oracle fixtures |
| `test_workflow*` | 5 | engine / nodes / templates / runs / artifact |
| `test_slow_*` | 4 | slow_sql mysql/oracle/dm + AI enrich |
| `test_excel_*` | 4 | excel_reader / form_analyzer / form_reader / uploads |
| `test_api_*` | 4 | api_integration / api_versioning / api_search / api_assets |
| `test_compare_*` | 3 | engine / schema / validation |
| `test_auth*` | 3 | login / mfa / refresh rotation |

其它单文件覆盖(`test_dialects` / `test_dbclients` / `test_db_statement_timeout` / `test_runner_streaming` / `test_jobs` / `test_resource_guard` / `test_scheduler` / `test_sensors` / `test_metrics` / `test_notifier` / `test_logging` / `test_history` / `test_trace_compare` / `test_search` / `test_project_authorization` / `test_download_token` / `test_metadata` / `test_schema_*` / `test_openlineage` / `test_threading_*` / `test_dm` 等)。

### 6.3 e2e 测试

`tests/e2e/`(Playwright,独立 Dockerfile + compose profile):
- `test_auth.py`
- `test_project_space.py`
- `test_workflow_templates.py`
- `conftest.py` + `__init__.py`

### 6.4 前端测试 `frontend/frontend/tests/`

10 个 vitest 文件:
- `i18n.test.js`(i18n key 对齐校验)
- `api.test.js`
- `stores/`: project / notice / auth / sqlTemplates / sqlWorkbench(5 个)
- `components/`: SqlEditor.alias / ErrorBoundary / ExplainPanel(3 个)

CLAUDE.md 自述基线 **98 passed**(本次扫描确认)。

### 6.5 业务规则覆盖摘要(便于 2.0 反查)

- **Compare 引擎**: `test_compare_engine` / `test_compare_schema` / `test_compare_validation` / `test_runner_streaming` / `test_result_writer*`
- **SQL guard**: `test_sql_guard` / `test_sql_preflight`
- **方言能力**: `test_dialects`(注册 + 5 方言 SQL 契约) / `test_dbclients` / `test_db_statement_timeout` / `test_dm`
- **Scenario 全套**: 13 个 `test_scenarios_*` 覆盖 DSL / generator / verifier / orchestrator / dialects / templating / ai_filler / faker
- **Lineage**: 12 个 `test_lineage_*` 覆盖 analyzer / batch / preprocess / semantic / columns / variables / dialects / aspects
- **作业流**: 5 个 `test_workflow*` 覆盖 engine / template / rerun / artifact
- **认证 / 权限**: `test_auth` / `test_mfa` / `test_project_authorization` / `test_download_token`
- **观测**: `test_metrics` / `test_logging` / `test_notifier` / `test_openlineage`

---

## 7. 技术栈与依赖

### 7.1 后端(`requirements.txt`)

**核心框架**:
- `fastapi>=0.110`
- `uvicorn[standard]>=0.27`
- `pydantic>=2.6`
- `jinja2>=3.1`
- `python-multipart>=0.0.9`
- `httpx>=0.27`(TestClient 依赖)

**数据处理**:
- `pandas>=2.2`
- `openpyxl>=3.1`(Excel)
- `pyarrow>=14.0`(Parquet 读写 + pandas IO)
- `sqlglot>=26.0`(SQL 解析 / 血缘 / format)
- `pyyaml>=6.0`(scenarios / lineage_group_rules)

**认证**:
- `bcrypt>=4.0`(口令 hash,不通过 passlib)
- `python-jose[cryptography]>=3.3`(JWT)
- `pyotp>=2.9`(TOTP / MFA)
- `cryptography`(Fernet + PyMySQL caching_sha2_password)

**调度**:
- `APScheduler>=3.10`
- `tzdata>=2024.1; sys_platform == "win32"`

**测试**:
- `pytest>=8.0`

**数据库驱动**(可选,实际安装见 `requirements.txt`):
- `dmPython`(已装)
- `pymysql`(已装)
- `oracledb`(注释)
- `ibm_db`(注释)

**AI**:
- `faker>=24.0`(Phase 14 ai_filler v3 locale fallback)
- 各 LLM provider 走 `httpx` 自实现(不依赖 openai-sdk / anthropic-sdk 等官方 SDK)— **未确认**所有 provider 是否纯 httpx 自实现

### 7.2 前端(`frontend/frontend/package.json`)

**框架**:
- `vue@^3.5.32`
- `vue-router@^4.6.4`(hash mode)
- `pinia@^3.0.4`(10 个 store)
- `vue-i18n@^11.4.0`(zh/en 双语)

**构建**:
- `vite@^8.0.10`
- `@vitejs/plugin-vue@^6.0.6`
- `tailwindcss@^3.4.18` + `postcss` + `autoprefixer`

**编辑器**:
- `@codemirror/{state,view,autocomplete,lang-sql,theme-one-dark}@^6.x` + `codemirror@^6.0.2`

**图可视化**:
- `@antv/g6@^5.1.0`(血缘图默认引擎)
- `cytoscape@^3.33.3` + `cytoscape-dagre@^2.5.0`(实验通道)

**工具**:
- `@vueuse/core@^14.2.1`
- `@tanstack/vue-virtual@^3.13.24`(大列表虚拟滚动)
- `lucide-vue-next@^1.0.0`(图标)
- `qrcode@^1.5.4` + `@types/qrcode`(MFA 二维码)

**测试 / TS**:
- `vitest@^4.1.5` + `@vitest/coverage-v8@^4.1.5` + `@vue/test-utils@^2.4.10` + `jsdom@^29.1.1`
- `typescript@^5.9.3` + `vue-tsc@^3.2.8`
- `openapi-typescript@^7.13.0`(`npm run schema:fetch` 从 `/openapi.json` 生 `src/types/api-schema.ts`)

### 7.3 前端目录结构(`frontend/frontend/src/`)

```
src/
├── api.ts              # apiGet<T> / apiJson<T> / apiForm 泛型封装
├── App.vue             # 顶部 useXxxStore() + storeToRefs + provide('app', {...}) 兼容
├── main.js             # createApp + router + i18n + pinia
├── style.css           # @tailwind + @layer base/components(.btn/.card/.pill 等)
├── assets/             # 图片资源
├── components/         # 通用组件(SqlEditor / LineageGraph / CommandPalette 等)
│   └── workflow/       # DAG canvas + 节点编辑器 + history panel
├── composables/        # useLineageGraphData 等组合式函数
├── i18n/               # zh / en JSON
├── layouts/            # AppShell / AppSidebar / AppTopBar
├── mock/               # workflow_meta.js
├── router/             # 路由表(hash mode)
├── stores/             # 10 个 Pinia store:notice/datasource/task/workflow/lineage/batch/history/bootstrap/auth/project
├── types/              # api-schema.ts(codegen) + api.ts(友好别名)
├── utils/              # 通用工具
└── views/              # 20 个路由级 view,全部 lazy load
    ├── account/
    ├── admin/          # 5 个 admin view(用户/审计/项目/AI 配置/调度器监控)
    ├── workbench/      # 数据对比 step 1-4 子 view(DataSourcePanel 等)
    ├── workflow/       # workflow list/detail/run
    └── sql-optimize/   # SQL 优化工作台(Phase 14 P0-1)
```

### 7.4 关键 env 变量(扫到的)

| 变量 | 用途 | 默认 |
|---|---|---|
| `DATAOPS_JWT_SECRET` | JWT HS256 签名 key | dev fallback + 启动 warning |
| `DATAOPS_CONFIG_SECRET` / `DATAOPS_SECRET_KEY` | Fernet 加密 key 源 | fallback `config/.dataops_secret.key` |
| `DATAOPS_DB_STATEMENT_TIMEOUT_SECONDS` | 全局 statement timeout | 900s |
| `DATAOPS_LOG_FORMAT` | json / text | text |
| `DATAOPS_REFRESH_TTL_SECONDS` | refresh token 寿命(`0` 关闭 refresh) | 未确认默认值 |
| `DATAOPS_COMPARE_AUTO_STREAM_BYTES` | 大任务自动 promote stream_compare 的阈值 | 1 GiB |
| `RESULTS_MIN_FREE_GB` | mid-run disk watermark | 5 |
| `RESULTS_MAX_DISK_USAGE_PERCENT` | mid-run disk watermark | 85 |
| `DM_HOME` / `DM_DLL_DIR` | DM driver DLL 路径 | — |
| `IBM_DB_HOME` / `DB2CLI_HOME` | DB2 driver DLL 路径 | — |
| `DATAOPS_ENV` | prod / dev 模式切换 | 未确认默认值 |

---

## 8. 未确认 / 留待 2.0 复查的项

> 文档生成时基于代码静态扫描得出,以下条目读到但不完全确定细节,2.0 移植前建议人工确认:

1. `app/scenarios/yml_importer.py` 具体用途(可能是 schema → yml 反向导入)
2. `app/sqlide/search.py` 是否仅 in-console 内容搜索
3. `services/slow_sql.py` 是否有专门 DB2 分析路径(目前看仅 MySQL/Oracle/DM 三方言)
4. `services/jobs.py:_load_jobs_from_disk` 重启后"运行中标 failed"的具体路径
5. 各 LLM provider 是否全部纯 `httpx` 自实现(无官方 SDK 依赖)
6. `RESULTS_MIN_FREE_GB` / `RESULTS_MAX_DISK_USAGE_PERCENT` 是否还有别的可调参数(扫到 2 个,可能不全)
7. `DATAOPS_ENV` / `DATAOPS_REFRESH_TTL_SECONDS` 等 env 默认值
8. `templates/` 顶层目录 6 个文件用途(Jinja2 模板,本次扫描未深入)
9. `samples/` 顶层目录文件用途
10. `init_db/` 是否还有非 docker-compose 场景用到

---

---

## 9. Lineage Aspect 体系(补充扫描)

> **背景**: 这一节澄清「lineage 解析产出」与「资产分类 aspect」是不是同一套。
> **核心结论**: **是两套完全独立的体系**,代码、存储、术语都不共享。

### 9.1 「lineage 解析 aspect」是否存在这个术语?

**代码里不存在 `aspect` 术语关联 lineage**。`grep -r "aspect" app/lineage/` 零结果。

CLAUDE.md 自述「analyzer.py 拆 12 个 aspect 模块」是**项目文档的描述性叫法**,实际代码组织是:
- `app/lineage/*.py` 19 个子模块按职责拆 — **没有 "aspect" 关键字**
- 解析产出 Pydantic schema 在 `app/models/lineage.py` — **没有 "aspect" 类 / 字段名**
- 跟 `app/services/asset_aspects.py`(资产分类)是字面意义两个不相关模块

### 9.2 两套对比表

| 维度 | 资产分类 aspect | lineage 解析产出 |
|---|---|---|
| **代码位置** | `app/services/asset_aspects.py` | `app/lineage/*.py` + `app/models/lineage.py` |
| **schema 定义** | `config/asset_aspects.yml`(yml 外置,加新类型不动表结构) | `app/models/lineage.py` Pydantic + Literal 闭集 |
| **存储** | SQLite `asset_aspects` + `asset_aspect_history` 表 | **不持久化为 lineage 专表**;通过 workload 落 JSON |
| **内置类型数** | **6 个**(owner/pii/sla/sensitive/tag/business_term) | 9 个 `Literal` 闭集 + 11 个 Pydantic model + 20 个 LineageReport 字段 |
| **用途** | 给"表"挂元数据标签(谁负责 / 是否 PII / SLA 等级 等),DataHub/Atlan 风格 governance | sqlglot 解析 SQL 后的结构化产出(表 / 列 / DML / 过程段 / 警告 / AI 兜底) |
| **API endpoint** | `/api/assets/aspects`(PUT/DELETE/search/history)+ `/api/assets/aspects/types` | `/api/lineage/analyze`(analyze_json) + `/api/lineage/batch` |
| **CRUD 权限** | editor+ inline 编辑(增删改),admin 看变更轨迹 | 只读 — analyze 调一次返一次,不存改 |
| **跨 schema 关系** | 完全独立模块 | 完全独立模块 |

### 9.3 `app/lineage/` 19 个子模块职责(每个一句话)

| # | 文件 | 职责 |
|---|---|---|
| 1 | `__init__.py` | 空 init(本次扫描未读到内容) |
| 2 | `analyzer.py` | 单脚本入口 `analyze_sql_lineage(sql, dialect, schema)` — 编排其它模块产出 `LineageReport` |
| 3 | `batch_analyzer.py` | 多文件 ETL 批量血缘 `analyze_lineage_batch()`(支持 `.sql` / `.txt` / `.zip`)— 产出 `LineageBatchReport` |
| 4 | `segments.py` | 存储过程分段抽取(BEGIN/END token 平衡,PL/SQL 控制流壳子跳过) |
| 5 | `columns.py` | 字段级 lineage 抽取(`insert_mappings` 形态) |
| 6 | `dml.py` | DML 语句解析(INSERT / UPDATE / MERGE / DELETE / CTAS / INSERT OVERWRITE / TRUNCATE) |
| 7 | `tables.py` | 表引用归一化(schema-qualified / alias / DBLink 等) |
| 8 | `roles.py` | 表角色标签(8 类 `TableRoleKind` Literal) |
| 9 | `aggregation.py` | DML 按目标表聚合(`aggregate_target_summary` + `collect_target_operations` + `collect_procedure_operations`),推断 `refresh_mode` |
| 10 | `grouping.py` | 业务分组规则匹配(基于 `config/lineage_group_rules.yml`,schema/basename/title 三类 matcher) |
| 11 | `semantic.py` | 把 `target_summary` / `table_roles` / `procedure_segments` / `parse_errors` / `dynamic_sql_segments` 收口成 `result["semantic_lineage"]` |
| 12 | `preprocess.py` | SQL 解析前归一化 11 处(全角标点 / `${name}` 模板变量 / `< =` / Oracle alias / `RETURNING INTO` 剥离 等) |
| 13 | `dialects.py` | `_resolve_dialect()` 方言路由到 sqlglot(dm/dameng → oracle / ob_mysql/oceanbase → mysql / ob_oracle → oracle) |
| 14 | `variables.py` | PL/SQL 变量提取(package_constant / package_variable / declare_constant / declare_variable + 模板变量) |
| 15 | `clauses.py` | SQL 子句拆分(joins / filters / group_by / unions) |
| 16 | `helpers.py` | 通用工具函数 |
| 17 | `_common.py` | 共享 utility(`raw_sql_aliases` / `unique_strings` 等) |
| 18 | `warnings.py` | 警告收集器 |
| 19 | `report.py` | `LineageBatchReport` 形态构造 |
| 20 | `graph.py` | `graph_edges` / `graph_groups` 构图 |

> 实际是 20 个 .py 文件(本次扫描),`__init__.py` 算一个。CLAUDE.md 提到的「12 个 aspect 模块」可能指其中实质做解析工作的子集(analyzer / segments / columns / dml / tables / roles / aggregation / grouping / semantic / preprocess / variables / clauses 数 12 个)。

### 9.4 lineage 解析产出的真正"aspect"(等价物)

代码里没用 `aspect` 命名,但**功能上等价于 aspect 的**是以下三层:

#### 9.4.1 9 个 `Literal` 闭集(`app/models/lineage.py` line 23-69)

```python
DmlType = Literal[
    "INSERT", "INSERT_OVERWRITE", "REPLACE",
    "CREATE_TABLE_AS", "CREATE_OR_REPLACE_TABLE_AS", "CREATE_TEMP_TABLE_AS",
    "UPDATE", "MERGE", "DELETE", "TRUNCATE",
]   # 10 取值

RefreshMode = Literal[
    "truncate_insert", "delete_insert", "delete_insert_partial",
    "merge", "update", "append", "mixed",
]   # 7 取值

TableRoleKind = Literal[
    "target", "intermediate", "source_fact", "remote_dblink",
    "config", "reference", "dimension", "filter",
]   # 8 取值

ParseStatus = Literal["parsed", "unsupported", "unknown"]
RuleConfidence = Literal["high", "medium", "low"]
AIConfidence = Literal["low", "medium"]    # 永不允许 high(6 不变量约束)
AIInferenceDmlType = Literal[
    "INSERT", "UPDATE", "MERGE", "DELETE", "CTAS", "TRUNCATE_INSERT",
]
AIInferenceSourceKind = Literal["parse_error", "dynamic_sql", "column_attribution"]
RiskLevel = Literal["high", "medium", "low"]
```

#### 9.4.2 11 个 Pydantic model(`app/models/lineage.py`)

| Model | 用途 |
|---|---|
| `TableRef` | 表引用(name / schema / database) |
| `ColumnRef` | 字段引用(name / table) |
| `ColumnEdge` | 字段级血缘单条边(target_column ← [source_columns],带 transform / confidence / reason) |
| `TargetOperation` | 单条写操作(order / target_table / dml_type / has_where / title) |
| `TargetSummary` | 按目标表聚合(insert_count / update_count / merge_count / delete_count / truncate_count / delete_before_insert / truncate_before_insert / refresh_mode / titles / procedure_origins) |
| `ProcessStep` | 存储过程内单段 step(segment_index / dml_keyword / target_table / line_start / line_end / preceding_comment / parse_status) |
| `AIInferredEdge` | AI 兜底推断的单条边(confidence 强制降级为 low/medium) |
| `AIColumnHint` | AI 字段归属推荐(unqualified column → suggested_table) |
| `AIInferenceResult` | `result["ai_inferred"]` 整体信封(edges / column_hints / warnings / trigger_count / filtered_count) |
| `LineageReport` | `analyze_sql_lineage()` 顶层结果(20 字段,详见 §9.4.3) |
| `LineageBatchReport` | `analyze_lineage_batch()` 顶层结果(file_count / files / table_edges / table_groups / script_edges / field_mappings / impact_analysis / dag / 等 15 字段) |

#### 9.4.3 `LineageReport` 顶层信封的 20 个字段

> 这是功能上最接近"lineage aspect"的概念 —— 解析后的每个字段就是一种 aspect。

```python
class LineageReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")  # envelope 允许新字段透传

    # 元信息
    statement_count: int                          # SQL 语句数

    # 表 / 列层面
    tables: list[dict[str, Any]]                  # 表引用列表
    columns: list[dict[str, Any]]                 # 字段引用列表
    insert_mappings: list[dict[str, Any]]         # 字段级 source → target 映射
    aliases: list[str]                            # 表别名

    # 写操作层面
    target_summary: list[TargetSummary]           # 按目标表聚合 + refresh_mode
    table_roles: list[dict[str, Any]]             # 表角色标签

    # SQL 子句
    joins: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    group_by: list[dict[str, Any]]
    unions: list[dict[str, Any]]

    # 动态 SQL / 存储过程
    dynamic_sql_count: int
    dynamic_sql_segments: list[dict[str, Any]]    # 三种识别路径
    procedure_segments: list[dict[str, Any]]      # BEGIN/END 平衡抽出来的段

    # 变量
    variables: list[dict[str, Any]]               # PL/SQL 变量(package_constant 等 4 kind)

    # 图
    graph_edges: list[dict[str, Any]]             # 表级血缘边
    graph_groups: list[dict[str, Any]]            # 业务分组

    # 警告 / 错误
    parse_errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    # 详情
    statements: list[dict[str, Any]]              # 每条 SQL 独立信息
    semantic_lineage: dict[str, Any]              # observations / risks / 分组DAG / targets / steps
    report: dict[str, Any] | None                 # 渲染态额外字段

    # AI 字段(不在这里建模,由 lineage_service._attach_ai_enrichment / _attach_ai_inference 单独注入,
    # envelope extra='allow' 透传)
    # - ai_enrichment   (summary / suggestions / risks / column_hints)
    # - ai_inferred     (AIInferenceResult — edges + column_hints + warnings)
```

### 9.5 存储位置

**lineage 解析结果不进 SQLite。** 持久化路径仅以下三条:

1. **lineage_script workload**(scenario 触发)→ `results/<run_id>.json`,run_id 形如 `lineage_script_<YYYYMMDDHHMMSS>_<8hex>`,由 `app/scenarios/recorder.py:_record_lineage_scripts` 写
2. **workflow lineage 节点** → `WorkflowRun.nodes[].output["lineage"]` 字段,随 WorkflowRun 整体落 `results/workflow_runs/<run_id>.json`
3. **`LineageBatchReport` 批量导出** → `app/services/lineage_service.py:_write_batch_exports` 写若干文件(具体路径 / 文件名 **未确认**)

**`services/lineage_index.py`** 是内存索引(从最近 50 个 workflow_run 聚合 `graph_edges` / `table_roles` / `target_summary`),TTL 300s + run 数变化失效,**不持久化**。admin `POST /api/lineage/graph/refresh` 强制失效。

`asset_aspects` 表跟 lineage 解析结果**零关系**,不共享 schema,不共享存储。

### 9.6 tests 覆盖

| 测试文件 | 覆盖的 aspect |
|---|---|
| `tests/test_lineage_models.py` | Pydantic schema(round-trip / Literal 取值校验 / extra 字段透传 / TableRef alias 双向 / AI confidence 强制降级为 low / LineageReport 信封默认值)— 18 个 `def test_` |
| `tests/test_lineage_analyzer.py` | analyzer 端到端(Oracle PL/SQL fixture 等) |
| `tests/test_lineage_report.py` | report.py 形态构造 |
| `tests/test_lineage_transform.py` | 各种 transform 类型识别 |
| `tests/test_lineage_grouping.py` | 业务分组规则匹配 |
| `tests/test_lineage_ai.py` / `test_lineage_ai_inference.py` / `test_lineage_ai_inference_async.py` | AI enrichment + inference + 异步化 |
| `tests/test_lineage_index.py` | lineage_index 服务端 BFS + TTL |
| `tests/test_lineage_graph_query.py` | `/api/lineage/graph/subgraph` 端点 |
| `tests/test_lineage_exporter.py` | 批量导出 |
| `tests/test_lineage_stress.py` | 大图压测 fixture |
| `tests/test_dm_lineage.py` | DM 方言专属 lineage 路径 |

**不存在的文件**: `tests/test_lineage_aspects.py` — 没有以 "aspect" 命名的 lineage 测试,印证了「lineage 解析没用 aspect 这个术语」的结论。

### 9.7 给 2.0 移植的关键提醒

- **如果 2.0 想统一称呼为 "aspect"**:`asset_aspects` 是动态可扩展的标签系统(yml schema + SQLite KV),`LineageReport` 字段是固定的 Pydantic schema(改字段要改 model);两者扩展机制完全不同
- **如果 2.0 复用 1.x 解析逻辑**:`app/lineage/` 19 个子模块 + `app/models/lineage.py` Pydantic schema 是一个内聚单元,迁过去就行;`asset_aspects.py` / SQLite 表是另一个独立单元
- **migrate_from_v1.py 注意**:lineage 解析结果**不需要迁** —— 每次跑都重算;只需要迁 `asset_aspects` + `asset_aspect_history` 两张 SQLite 表
- **`LineageReport.semantic_lineage`** 是个 `dict[str, Any]`(未 typed),里面包含 `observations / risks / targets / steps` 等子结构,具体形态由 `app/lineage/semantic.py` 构造 — **本次扫描未深入到字段级,2.0 移植时建议读 `semantic.py` 源码**

---

**文档生成时间**: 2026-05-28
**Commit**: ec9abe6
**扫描覆盖**: 顶层 + `app/` 全部子包 + 关键 service 文件 + 4 个方言子类 + Pydantic model + 配置 example + SQLite schema + tests/ 分类 + 前后端依赖锁定 + **§9 lineage aspect 体系补充扫描**
