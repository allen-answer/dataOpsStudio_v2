# Oracle / OceanBase(Oracle 模式、MySQL 模式)全模块适配设计

状态:草案,待评审。基线:`main` @ `4496259`。

本稿只基于代码阅读和 OceanBase / Oracle 公开文档,**尚未连过任何真实实例**。文中标
**〔待验证 Vn〕** 的结论全部由 P0 连通 POC 关闭(清单见 §13),POC 推翻的地方回改本稿后再
进入编码。

---

## 1. 目标与非目标

### 1.1 目标

让下面三种业务库在**所有模块**达到与 MySQL / DM 同档(Certified)的支持:

| 简称 | 含义 | 新 `DbType` 值 |
|---|---|---|
| Oracle | Oracle Database(版本下限见 §4.1.1) | `oracle`(已有) |
| OB-MySQL | OceanBase MySQL 模式租户 | `oceanbase_mysql`(新增) |
| OB-Oracle | OceanBase Oracle 模式租户(仅企业版) | `oceanbase_oracle`(新增) |

“所有模块”指:数据源管理、元数据浏览与同步、SQL 工作台(作业式执行、执行计划、展开
`*`、自动补全、导出)、会话式控制台(Session Broker)、数据比对(含库内哈希下推、跨库比
对、差异定位 SQL)、血缘、AI 助手、工作流。

### 1.2 非目标

- 写操作(DML/DDL)。会话控制台的写阶段(ADR-0023 阶段 B)不在本稿范围,本稿只保证新方言
  的会话缝**不把只读焊死**(与现有 MySQL/DM 缝一致)。
- OceanBase 社区版的 Oracle 模式(社区版没有 Oracle 模式)。
- OB 的租户/集群运维能力(建租户、资源单元等)。
- Oracle RAC / Data Guard 的拓扑感知。连接串能连上即可,不做故障切换编排。

---

## 2. 现状盘点(main @ 4496259)

### 2.1 按模块

| 模块 | Oracle | OB-MySQL(借 `mysql` 类型) | OB-Oracle |
|---|---|---|---|
| 适配器 | `OracleAdapter` Preview,thin 模式 | `MySQLAdapter` 原样 | 无 |
| 测试连接 | 有 | 能连(用户名带租户) | 连不上 |
| 元数据 | `ALL_*` 视图已实现 | `information_schema`,系统库未排除 OB 内置库 | 无 |
| 建表 DDL | `get_table_ddl=False` | `SHOW CREATE TABLE`〔待验证 V9〕 | 无 |
| 执行计划 | `explain=False` | `EXPLAIN` 能跑,但返回是 OB 文本计划 | 无 |
| 作业式执行 | 后端可跑;前端白名单没放开 | 可用 | 无 |
| 展开 `*`、自动补全 | 前端白名单没放开 | 可用 | 无 |
| 会话控制台 | 无(只 MySQL/DM) | 走 MySQL 缝,超时/取消未验证 | 无 |
| 比对:库内哈希下推 | 无(回退客户端逐行哈希) | 走 MySQL 实现〔待验证 V11〕 | 无 |
| 比对:客户端逐行 | 有 | 有 | 无 |
| 导出 SQL / 差异 SQL | 双引号加大写 | 正常(按 `mysql`) | 无 |
| 血缘 / AI | 方言 `oracle` | 方言 `mysql` | 无 |

### 2.2 代码里按 `db_type` 硬分支的位置

新增类型时这些地方都要动,也是本稿要收敛的对象(§3.2):

| 位置 | 现状 | 问题 |
|---|---|---|
| `app/dbclients/sql_build.py:22/32/42` | `is DbType.MYSQL` 走反引号/`LIMIT`,其余双引号加大写/`FETCH FIRST` | OB-MySQL 会落进 else |
| `app/infrastructure/result_export.py:404` | `db_type == "mysql"` | 同上 |
| `app/dbclients/query_limit.py:27-34` | `_DIALECTS` 映射 sqlglot 方言 | 新类型 `KeyError` → 拒绝执行 |
| `app/domain/compare_sql.py:39-43` | `_DIALECTS` | 同上 |
| `app/api/routes/core.py:8566` | `_sqlglot_dialect()` 字符串归一 | 新类型 400 |
| `app/api/routes/core.py:10992/11000` | 缺省回落 `mysql` | — |
| `app/domain/ai_upstream_locator.py:222` | 方言映射字典 | 新类型丢方言 |
| `app/services/ai/sql_assistant.py:277` | `== "mysql"` | 同上 |
| `app/domain/lineage/dialects.py:26-31` | 方言白名单 | 同上 |
| `app/worker.py:3698` | `_can_use_db_hash` 写死 `{MYSQL, DM}` | 绕过了 `capabilities.compare_db_hash` |
| `app/worker.py:1021` | `source_db_type` 缺省 `"mysql"` | — |
| `app/broker/wiring.py:35/55/63` | `SESSION_CAPABLE_DB_TYPES` 与工厂分支 | — |
| `app/api/routes/sessions.py:162` | 引用上面的集合 | — |
| `app/dbclients/factory.py` | `if/elif` 派发 | — |
| 前端 `views/SqlWorkspaceView.vue:146/148` | `SUPPORTED_EXECUTION_DB_TYPES` / `SUPPORTED_TOOL_DB_TYPES` | 不含 oracle |
| 前端 `utils/sqlIntelligence.ts:17` | `METADATA_DB_TYPES` | 不含 oracle |
| 前端 `composables/useConsoleSessions.ts:30` | `SESSION_CAPABLE_DB_TYPES` | 与后端重复维护 |
| 前端 `views/DatasourcesView.vue:317/564` | 端口缺省、类型列表 | — |
| 前端 `views/LineageView.vue:87/788` | 方言下拉 | — |
| 前端 `api/types.ts:23` | `DbType` 联合类型 | — |

另外:数据源表单**没有暴露 `extra`**(`DatasourcesView.vue:392` 恒提交 `extra: {}`),所以
Oracle 的 `service_name` / `dsn` 目前只能靠 `database` 字段兜。

### 2.3 评估阶段没注意到、本次读代码发现的问题

1. **Oracle `DATE` 被归为纯日期**(`oracle_types.py:15/56`,沿用 DM 口径)。Oracle 的
   `DATE` 带时分秒,比对时会按 `YYYY-MM-DD` 归一,**两行只差时间会被判成一致**,这是比对结
   果的正确性问题,不只是精度问题。见 §6.3。
2. **`compare-normalization-spec.md` 明令禁止 `ORA_HASH`** 作为跨库比对依据。需求评估里提
   的“Oracle 用 `ORA_HASH`”作废,统一用 MD5(§6.1)。
3. **Oracle 拼接上限**:SQL 里 `||` 拼出的 `VARCHAR2` 默认上限 4000 字节
   (`MAX_STRING_SIZE=STANDARD`),多列宽表的哈希载荷会报 `ORA-01489`。DM 的实现没有这个约
   束,照搬会在 Oracle 上炸。见 §6.1.2。
4. **python-oracledb 的 thin / thick 是进程级的**:一个进程启用 thick 后就不能再用 thin。
   “11g 走 thick”只能做成部署级开关,不能按数据源选。见 §4.1.1。

---

## 3. 总体设计

### 3.1 数据源类型:两个新枚举,而不是复用 `mysql` 或单一 `oceanbase`

```python
class DbType(StrEnum):
    MYSQL = "mysql"
    ORACLE = "oracle"
    DM = "dm"
    DB2 = "db2"
    POSTGRESQL = "postgresql"
    OCEANBASE_MYSQL = "oceanbase_mysql"    # 新增
    OCEANBASE_ORACLE = "oceanbase_oracle"  # 新增
```

取舍:

- **不复用 `mysql`**:OB-MySQL 的超时变量、错误码、执行计划格式、系统库、`KILL` 语义都和
  MySQL 不同(§4.2),塞进 `MySQLAdapter` 的 `if is_oceanbase` 会把 MySQL 的 Certified
  路径搅浑。
- **不做单一 `oceanbase` + `extra.mode`**:系统里几乎所有方言分支都以 `db_type` 为键
  (§2.2),模式藏进 `extra` 会让每个分支都要多读一层。两种模式的 SQL 方言完全不同,本来
  就该是两个类型。
- **落库零迁移**:`datasources.db_type` 是 `String(32)`、没有 CHECK 约束
  (`app/db/models.py:196`、`0001_initial.py:123`),新值直接可写。
- **存量兼容**:已经用 `mysql` 类型建的 OB 数据源继续按 MySQL 跑,不自动改类型。测试连接
  时若 `SELECT VERSION()` 返回含 `OceanBase`,在结果里提示“建议改为 OceanBase(MySQL 模式)
  类型”(只提示,不改)。

### 3.2 方言族:把散落的硬分支收敛到一处

新增 `app/domain/dialect.py`(纯领域,不 import 驱动):

```python
class IdentifierStyle(StrEnum):
    BACKTICK = "backtick"          # MySQL 族:`name`,原样大小写
    DOUBLE_QUOTE_UPPER = "dq_upper"  # Oracle 族:"NAME",目录大写存储
    DOUBLE_QUOTE = "dq"            # PG:"name",保持原样(现状 PG 也被大写,见下)

@dataclass(frozen=True)
class DialectTraits:
    family: Literal["mysql", "oracle", "postgres", "db2"]
    sqlglot: str | None            # query_limit / compare_sql / API 归一共用
    lineage: str                   # 血缘方言名
    identifier_style: IdentifierStyle
    limit_style: Literal["limit", "fetch_first", "rownum"]
    paramstyle: Literal["pyformat", "named", "qmark"]
    empty_string_is_null: bool     # Oracle 族 True,比对规则推荐用(§6.4)

DIALECT_TRAITS: dict[DbType, DialectTraits] = {...}

def traits(db_type: DbType | str) -> DialectTraits: ...
```

| DbType | family | sqlglot | identifier | limit | paramstyle | `''`=NULL |
|---|---|---|---|---|---|---|
| mysql | mysql | mysql | backtick | limit | pyformat | 否 |
| oceanbase_mysql | mysql | mysql | backtick | limit | pyformat | 否 |
| oracle | oracle | oracle | dq_upper | fetch_first(11g 为 rownum) | named | 是 |
| oceanbase_oracle | oracle | oracle | dq_upper | fetch_first〔待验证 V7〕 | pyformat(走 MySQL 协议驱动) | 是 |
| dm | oracle | oracle | dq_upper | fetch_first | qmark | 否(DM 默认区分)〔沿用现状〕 |
| db2 | db2 | None / oracle | dq_upper | fetch_first | qmark | 否 |
| postgresql | postgres | postgres | dq_upper(维持现状) | fetch_first | pyformat | 否 |

改造原则:**行为不变的纯重构先行**(单独一个 PR,§12 的 PR-0),把 §2.2 表里的后端硬分支
全部换成 `traits(db_type).xxx`,现有 5 个类型的输出逐字节不变(用现有单测 + 新增的“快照对
照”单测证明)。PG 被大写这一现状不在本稿修,`DOUBLE_QUOTE` 只预留。

`limit_style="rownum"` 只在部署启用 Oracle thick 且实例 < 12c 时由适配器在运行期覆盖
(§4.1.1),`traits` 给的是类型级缺省。

### 3.3 能力下发:前端不再硬编码类型白名单

现在前端四处白名单(§2.2)和后端能力矩阵各自维护,新增类型要改两边,且“放开执行”需要单独
拍板时只能改前端常量。改为:

- `DatasourceResponse` 增加只读字段 `capabilities`:

  ```json
  {
    "execute": true, "explain": true, "ddl": true, "metadata": true,
    "sql_tools": true, "session_console": true, "compare_db_hash": true,
    "maturity": "certified" | "preview"
  }
  ```

  由后端按 **适配器类 `capabilities` × 会话缝可用性 × `operation_policy`** 计算,不连库。
- `maturity` 来自适配器类上的新类属性 `MATURITY`。“放开执行需单独 PR 人拍板”(现
  `SqlWorkspaceView.vue:145` 注释)的治理点,移到后端一处:**Preview 档的 `execute` 由一个
  显式的后端常量 `PREVIEW_EXECUTION_ALLOWED: frozenset[DbType]` 控制**,改它仍需人拍板。
- 前端 `SUPPORTED_EXECUTION_DB_TYPES` / `SUPPORTED_TOOL_DB_TYPES` / `METADATA_DB_TYPES` /
  `SESSION_CAPABLE_DB_TYPES` 全部删掉,改读 `datasource.capabilities`。老后端没有该字段时
  前端回落到旧常量(灰度期保留一版)。

### 3.4 连接参数(`extra`)与数据源表单

`extra` 落 `datasources.capability_profile`,按类型定义白名单字段(后端 pydantic 校验,
未知键拒绝,防止把密码塞进 `extra`——R2):

| 类型 | 字段 | 说明 |
|---|---|---|
| oracle | `service_name` | 缺省取 `database` |
| oracle | `sid` | 与 `service_name` 二选一,生成 `(CONNECT_DATA=(SID=...))` |
| oracle | `dsn` | 整串覆盖(Easy Connect / TNS 描述符),现有 |
| oceanbase_* | `tenant` | 必填 |
| oceanbase_* | `cluster` | 走 OBProxy 时必填 |
| oceanbase_* | `access_mode` | `proxy`(缺省,端口 2883)/ `direct`(2881) |
| oceanbase_mysql | `charset` / `ssl` | 同 MySQL |
| oceanbase_oracle | `charset` | 缺省 `utf8mb4` |

**OB 用户名拼装**在适配器内完成,表单里 `username` 只填纯用户名:

- `proxy`:`{user}@{tenant}#{cluster}`
- `direct`:`{user}@{tenant}`

若用户在 `username` 里已经写了 `@`,视为已拼好的完整串原样使用(兼容存量 `mysql` 类型数
据源迁过来的写法),此时 `tenant` / `cluster` 可省。

前端表单:按类型动态显示上述字段;端口缺省 `oceanbase_*` 取 2883,切到 `direct` 时若端口仍
是缺省值则改 2881(沿用 `DatasourcesView.vue:323-328` 的“只改缺省端口”逻辑)。

---

## 4. 适配器设计

### 4.1 Oracle(P1:Preview → Certified)

#### 4.1.1 版本下限与驱动模式

- **缺省:thin 模式,支持 Oracle 12.1 及以上**(现状,依赖已在 `uv.lock`:oracledb 4.0.1,
  已随 Win10/Server2019/Mint 包分发)。
- **11g(11.2)**:thin 不支持,需要 thick 模式 + Oracle Instant Client。由于 oracledb 的
  模式是**进程级**的,设计为部署级开关:
  - 环境变量 `DATAOPS_ORACLE_CLIENT_DIR` 指向 Instant Client 目录;设置后 API 与 worker
    进程启动时调用一次 `oracledb.init_oracle_client(lib_dir=...)`,该进程内所有 Oracle 连
    接都走 thick。
  - Instant Client **不进安装包**:包体有不许增长的基线门槛(`docs/packaging.md`),且许可
    条款需另行确认。由部署方自行放置,README 写明步骤。
  - thick 下 12c+ 实例照常可用,所以“有 11g 的现场整体开 thick”即可,不存在混用问题。
  - 若现场没有 11g,本开关不实现,只在文档里记下限。**是否实现取决于你给的版本信息。**
- 版本探测:`test_connection` 现已读 `product_component_version`,结果额外缓存主版本号,
  供分页语法(11g 用 `ROWNUM` 包裹)和哈希函数选型(§6.1.2)使用。

#### 4.1.2 能力补齐

| 能力 | 方案 | 权限前提 |
|---|---|---|
| `explain` | `EXPLAIN PLAN SET STATEMENT_ID = :sid FOR <sql>` → `SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', :sid, 'TYPICAL'))` → `ROLLBACK` | 10g 起 `PLAN_TABLE` 是公共同义词指向会话级临时表,普通账号可用;仍受 `operation_policy.allow_oracle_plan_table` 闸门(已有字段,缺省 False) |
| `get_table_ddl` | `DBMS_METADATA.GET_DDL('TABLE', UPPER(:t), UPPER(:s))`,先 `DBMS_METADATA.SET_TRANSFORM_PARAM` 关掉 `STORAGE`/`SEGMENT_ATTRIBUTES` | 查他人 schema 需 `SELECT_CATALOG_ROLE`;无权限时报结构化错误 `ddl_permission_denied`,不降级为空 |
| `compare_db_hash` | §6.1.2 | 12c+ 无需授权 |
| `server_side_cancel`(作业路径) | 维持 False;作业路径继续软取消 + `call_timeout`。真取消只在会话控制台做(§5) | — |

`PlanNode` 形状:沿用 MySQL/DM 的 `details={"rows": [...]}`,行是 `DBMS_XPLAN` 的文本行;
前端计划面板对 `oracle` 族按等宽文本渲染(OB 同,§4.2.4)。

#### 4.1.3 其他

- **类型映射修正**:`oracle_types` 中 `DATE → DATETIME`(而不是 DATE)。影响面:比对把
  Oracle `DATE` 按秒级时间戳归一(§6.3);元数据展示不变(`driver_type` 仍显示 `DATE`)。
  DM 不跟改(DM 的 `DATE` 确实只有日期)。
- **错误分类**:新增 `OracleErrorClassifier`,按 `ORA-` 数值码分类(登录失败 1017、
  监听不可达 12541/12514、取消 1013、超时 DPY-4024 / ORA-03156、连接死亡 3113/3114/3135
  等),全集在 POC 中按真库回报核对〔待验证 V3〕。
- **系统 schema 清单**(`oracle_adapter.py:69-105`)保留,补 `PDBADMIN`、`APEX_*` 前缀
  匹配。

### 4.2 OB-MySQL(P2)

#### 4.2.1 适配器形态

`OceanBaseMySQLAdapter(MySQLAdapter)`,只覆盖差异点;`MySQLAdapter.__init__` 里的
`db_type is not DbType.MYSQL` 校验改为类属性 `_ACCEPTED_DB_TYPES`,子类放宽。

| 覆盖点 | MySQL 现状 | OB-MySQL |
|---|---|---|
| 用户名 | 原样 | §3.4 拼装 |
| 语句超时 | `SET SESSION MAX_EXECUTION_TIME`(毫秒,失败静默) | `SET SESSION ob_query_timeout = <微秒>`;OB 对 `MAX_EXECUTION_TIME` 的支持〔待验证 V5〕,不依赖 |
| 事务超时 | — | 只读查询不开事务;如需显式设 `ob_trx_timeout` 留到写阶段 |
| 系统库排除 | `information_schema/mysql/performance_schema/sys` | 再加 `oceanbase`、`__public`、`__recyclebin`、`LBACSYS`、`ORAAUDITOR`〔待验证 V6〕 |
| 版本串 | `MySQL {VERSION()}` | `OceanBase {ob_version()}`(4.x 有 `ob_version()`;3.x 从 `VERSION()` 解析)〔待验证 V1〕 |
| `explain` | `EXPLAIN <sql>` 返回表格行 | `EXPLAIN <sql>` 返回单列文本计划;同样放进 `details.rows`,前端按文本渲染 |
| DDL | `SHOW CREATE TABLE` | 同,输出含 OB 专有子句(分区、`COMPRESSION` 等),原样返回 |
| 哈希下推 | `build_mysql_compare_hash_plan` | 复用,前提是 `MD5`/`CONV`/`CONCAT_WS`/`DATE_FORMAT`/`CAST ... CHARACTER SET ... COLLATE` 在 OB 上输出与 MySQL 逐字节一致〔待验证 V11〕;不一致的函数单独覆盖 |

#### 4.2.2 流式读取

PyMySQL `SSCursor` 在 OB 上可用(协议层一致);OBProxy 对长时间不取数的流式结果集有空闲
超时〔待验证 V8〕,`cursor_max_hold_seconds` 维持现值即可兜住。

### 4.3 OB-Oracle(P3)

这是三者里唯一需要新驱动接线的。**协议是 MySQL 协议,SQL 是 Oracle 方言。**

#### 4.3.1 驱动选型(需你拍板,§12)

| 方案 | 做法 | 新依赖 | 离线包影响 | 风险 |
|---|---|---|---|---|
| **A(推荐先试)** | PyMySQL 握手时 `client_flag` 置 OB 的 Oracle 模式能力位(bit 27),其余照常 | 无 | 无 | 能力位语义仅见于 OB 社区资料与 pyoceanbase 源码,需真库证明;扩展类型要自己解码 |
| B | 社区包 `pyoceanbase`(PyMySQL 分支,原理同 A) | 1 个纯 Python 包 | 很小 | 单人维护、2026-09 才 1.1.0;要锁版本和哈希 |
| C | oracledb thick / cx_Oracle + OB 的 OBCI(libobclient) | C 库 | 大,且 Windows 版供应不确定 | 与 Oracle thick 同属进程级,会和 §4.1.1 冲突 |
| D | JayDeBeApi + OB JDBC | JVM | 很大 | 部署过重 |

方案 A 若成立,与 B 本质相同,只是我们自己掌控那几行握手代码,不引入单人维护的依赖。
B 作为 A 失败时的备选。C、D 不进本轮。

为让 A/B 可替换,OB-Oracle 的驱动接线收在一个模块里:

```text
app/dbclients/oceanbase_oracle_driver.py
    connect(conn_info, password, *, timeouts) -> DB-API 连接   # A 或 B 的唯一实现点
    escape_literal(value) -> str                                # §4.3.3
    decode_ob_types(conn)                                        # 注册扩展类型解码器(§4.3.4)
```

适配器、会话缝、比对只调用这个模块,不直接碰 PyMySQL 的 Oracle 模式细节。

#### 4.3.2 适配器形态

`OceanBaseOracleAdapter`:**SQL 文本层复用 Oracle,执行层复用 MySQL 协议**。

为此把 `oracle_adapter.py` 里的目录查询 SQL(`ALL_USERS` / `ALL_TABLES` / `ALL_VIEWS` /
`ALL_TAB_COLUMNS` / `ALL_CONSTRAINTS` / `ALL_IND_COLUMNS` 等)抽到
`app/dbclients/oracle_catalog.py`,用占位符模板 + 渲染函数输出两种绑定风格:

```python
LIST_TABLES = CatalogSql(
    text="SELECT ... FROM ALL_TABLES WHERE OWNER = {owner} UNION ALL ...",
    params=("owner",),
)
LIST_TABLES.render("named")    # -> ... OWNER = :owner        (oracledb)
LIST_TABLES.render("pyformat") # -> ... OWNER = %(owner)s     (PyMySQL)
```

OB-Oracle 目录视图的列集合与 Oracle 不完全一致〔待验证 V6〕,有出入的查询在 `oracle_catalog`
里按 `variant="oceanbase"` 分叉,不在适配器里拼字符串。

| 能力 | OB-Oracle 方案 |
|---|---|
| 测试连接 | `SELECT 1 FROM DUAL`;版本 `SELECT ob_version() FROM DUAL`〔待验证 V1〕 |
| 元数据 | `oracle_catalog` 的 `pyformat` 渲染;系统 schema 加 `SYS`/`LBACSYS`/`ORAAUDITOR`/`PUBLIC` |
| 语句超时 | `SET ob_query_timeout = <微秒>`(Oracle 模式下也是系统变量)〔待验证 V5〕 |
| 流式读 | PyMySQL `SSCursor` |
| `explain` | `EXPLAIN <sql>`(OB 两种模式通用);`EXPLAIN PLAN FOR` + `DBMS_XPLAN` 是否支持〔待验证 V10〕,不依赖 |
| DDL | `DBMS_METADATA.GET_DDL` 〔待验证 V9〕,不行则 `SHOW CREATE TABLE`(OB 扩展) |
| 哈希下推 | Oracle 族实现 + OB 函数可用性探测(§6.1.3) |
| 分页 | `FETCH FIRST n ROWS ONLY`〔待验证 V7〕,不行则 `ROWNUM` |

#### 4.3.3 绑定参数转义(必须处理的坑)

PyMySQL 的参数是**客户端插值**:`%s` 被替换成转义后的字面量。它的字符串转义默认按 MySQL
规则把 `\` 变成 `\\`;只有服务端在握手状态里报告 `NO_BACKSLASH_ESCAPES` 时才只做 `'`→`''`。
Oracle 方言里反斜杠不是转义符,如果 OB-Oracle 不报这个状态位,**含反斜杠的绑定值会被改写**
(比对的分段边界、元数据查询的 owner 名都会受影响)。

处理:`oceanbase_oracle_driver.escape_literal` 固定按 Oracle 规则转义(只双写单引号),连
接建立后把 PyMySQL 连接的 `escape_string` 换成它;POC 里用含 `\`、`'`、`%`、中文的值往返
验证〔待验证 V4〕。

#### 4.3.4 扩展类型解码

OB 在 MySQL 协议里给 Oracle 模式类型分配了扩展类型码(公开源码中约为 200–211:带时区/本地
时区/纳秒时间戳、RAW、INTERVAL YM/DS、NUMBER_FLOAT、NVARCHAR2、NCHAR、UROWID、BLOB、
CLOB)。PyMySQL 不认识这些码,会按文本或字节原样返回。

新增 `app/dbclients/oceanbase_oracle_types.py`:

- 类型码 → `ColumnType` 映射(与 `oracle_types` 的口径对齐,`DATE → DATETIME`);
- 文本值 → Python 值的解码器(时间戳按 OB 返回格式解析,时区类保留偏移);
- 具体码值、文本格式以 POC 抓到的 `cursor.description` 和返回值为准〔待验证 V2〕。

Oracle `NUMBER` 在 OB-Oracle 里走 `NEWDECIMAL`(246),PyMySQL 已解成 `Decimal`;
`scale` 从 `description` 读,沿用 `oracle_types` 的“scale==0 归 INTEGER”规则。

---

## 5. 会话控制台(Session Broker)

现状只有 MySQL/DM(`app/broker/wiring.py:35`)。三种新方言各加一对“会话连接 + 控制通道”,
放进 `app/dbclients/interactive/`,接口不变(`BaseInteractiveConnection` /
`BaseCancelChannel`)。

| 方言 | 会话标识(marker) | 硬取消 | 破坏性兜底 | 服务端语句超时 | 取消/超时确认码 |
|---|---|---|---|---|---|
| OB-MySQL | `SELECT CONNECTION_ID()` | `KILL QUERY <id>` | `KILL <id>` | `ob_query_timeout`(微秒) | 1317 取消;OB 超时码(4012 族)〔待验证 V3〕 |
| OB-Oracle | `SELECT SYS_CONTEXT('USERENV','SID') FROM DUAL`〔待验证 V12〕 | `KILL QUERY <id>`(OB 扩展语法,Oracle 模式可用性待验证) | `ALTER SYSTEM KILL SESSION`〔待验证 V12〕 | `ob_query_timeout` | 按 POC |
| Oracle | `SYS_CONTEXT('USERENV','SID')` + `SERIAL#` | **进程内** `Connection.cancel()`(见下) | `ALTER SYSTEM KILL SESSION 'sid,serial#'`,需权限,探测不通即 `DEGRADED` | 无语句级服务端超时;`call_timeout` 作保险带 | ORA-01013 取消;DPY-4024 超时 |

**Oracle 的取消走进程内,不走控制连接 SQL**:

- `ALTER SYSTEM CANCEL SQL` 要 18c+ 且需 `ALTER SYSTEM` 权限,生产只读账号基本没有。
- python-oracledb 的 `Connection.cancel()` 正是设计成**由另一线程**打断本连接正在执行的调用,
  不要任何权限。会话连接与控制 lane 同在 API 进程内,条件天然满足。
- 协议改动:`InteractiveConnection` 增加可选方法 `local_cancel() -> bool`,Oracle 实现调
  `conn.cancel()`,其他方言返回 False。broker 的取消阶梯在“硬取消”一级先试
  `local_cancel()`,不行再走 `CancelChannel.cancel(marker)`。MySQL/DM 行为不变。
- `OracleCancelChannel` 只负责破坏性兜底(`KILL SESSION`)与权限探测;探测不通时如实上报
  `DEGRADED`,但由于 `local_cancel` 可用,前端提示文案区分“可取消,但兜底断开需 DBA 权限”。

OB 经 OBProxy 时,`CONNECTION_ID()` 返回的是代理侧会话号;`KILL QUERY` 能否被代理正确路由
到后端 observer〔待验证 V12〕。不行就只能在 `direct` 模式开硬取消,`proxy` 模式降级。

错误分类器:`OceanBaseMySQLErrorClassifier`(继承 MySQL 码表再补 OB 码)、
`OceanBaseOracleErrorClassifier`、`OracleErrorClassifier`(与 §4.1.3 共用码表)。仍然**只按
数值码分类,不看消息文本**(`interactive/protocol.py` 的既定原则)。

---

## 6. 数据比对

### 6.1 库内哈希下推

规范(`docs/design/compare-normalization-spec.md`)要求各库对**同一规范化载荷**算 MD5,取前
8 字节为无符号整数,段内求和。新方言只要能写出逐字节相同的载荷与 MD5,就能和 MySQL/DM/客户端
互比。

#### 6.1.1 实现矩阵

| 方言 | 载荷构造 | MD5 | hex → uint64 | 来源 |
|---|---|---|---|---|
| MySQL | `CONCAT_WS` | `MD5()` | `CONV` | 现有 `mysql_compare` |
| OB-MySQL | 同 MySQL | 同 | 同 | 复用,V11 验证 |
| DM | `\|\|` | `DBMS_CRYPTO.HASH` | `TO_NUMBER(...,'XXXXXXXX')` / 展开式 | 现有 `dm_compare` |
| Oracle | `\|\|` | `STANDARD_HASH(x,'MD5')`(12.1+,无需授权);11g 用 `DBMS_CRYPTO`(需授权)否则客户端 | 复用 DM 的 `hex_to_uint64_expression` | 新 `oracle_compare` |
| OB-Oracle | `\|\|` | `STANDARD_HASH` / `DBMS_CRYPTO` / `MD5()` 三选一,按探测〔待验证 V11〕 | 同上 | 新 `oracle_compare`(variant) |

`oracle_compare.py` 从 `dm_compare.py` 抽公共部分(载荷拼接、数值 `TO_CHAR` 格式模型、时间
格式化、hex→uint64),DM 与 Oracle 各自只保留 MD5 函数与降级原因。抽取后 DM 的输出 SQL
逐字节不变(现有 `test_dm_compare_hash.py` 保护)。

#### 6.1.2 Oracle 特有降级原因

- **载荷超长**:按列元数据估算载荷最大字节数(列 `CHAR_LENGTH`/`DATA_LENGTH` × 字符集
  最大字节数 + 分隔符 + 长度前缀),超过 4000(`MAX_STRING_SIZE=STANDARD`)即降级客户端哈希,
  原因写 `oracle payload may exceed VARCHAR2 limit`。适配器在首次构造计划时读一次
  `V$PARAMETER` 的 `max_string_size`(无权限则按 STANDARD 保守处理)。
- **`CLOB`/`NCLOB`/`LONG` 列**:归 `STRING` 但不能进 `STANDARD_HASH`,降级。
- **非 UTF-8 规范化字符集**:同 DM 现有规则。
- 11g 且无 `DBMS_CRYPTO` 执行权限:整表降级。

#### 6.1.3 能力探测

与 DM 的 `_resolve_hex_mode` 同款:每个适配器实例(即每个作业)首次构造计划时跑一次极小探测
SQL(`SELECT STANDARD_HASH('a','MD5') FROM DUAL` 等),选定函数;全部失败返回
`CLIENT_ROW_HASH` 并写明原因。**不静默用引擎缺省**(规范 Degradation 节)。

#### 6.1.4 worker 改为读能力

`app/worker.py:3698` 的 `db_type in {MYSQL, DM}` 改为
`self._adapter.capabilities.compare_db_hash`,单键、表引用等前置条件保持不变。这样新增方言只
需在适配器里打开能力,不再改 worker。

### 6.2 跨库比对:规范化口径的差异

| 差异 | 现象 | 处理 |
|---|---|---|
| Oracle 族 `''` 即 NULL | MySQL 侧 `''` 与 Oracle 侧 NULL 被判不同 | 对端含 Oracle 族时,建任务界面**推荐**勾 `empty_as_null`(依据 `traits.empty_string_is_null`),不强制 |
| Oracle `DATE` 带时间 | 现映射成 DATE,时间被吃掉,漏报差异 | §4.1.3 改映射为 DATETIME;MySQL `DATE` 对 Oracle `DATE` 时,比对前提示“精度不同,建议把 `timestamp_precision` 设为 0 并确认 MySQL 侧为 DATETIME” |
| `CHAR(n)` 补空格 | Oracle 返回补齐空格,MySQL 缺省去尾随空格 | 现有 `trim_strings` 会同时去首尾空格,范围偏大。新增规则 `rtrim_char`(只去尾随空格、只作用于 CHAR 列),需改规范与三方实现,**列为 P4 待决项** |
| `NUMBER` 无精度 | 可能带很长小数 | 现有 `numeric_scale` 已覆盖(四舍五入到固定位数) |
| `BINARY_FLOAT/DOUBLE` | 浮点表示差异 | 归 FLOAT,走现有 ROUND 规则 |
| 标识符大小写 | Oracle 族目录大写,MySQL 族原样 | 列映射按不区分大小写匹配(现有推断逻辑,P4 用黄金用例确认) |
| 时区时间戳 | `TIMESTAMP WITH TIME ZONE` | 规范化前统一转 UTC;无法下推时降级 |

这些只影响“跨族”比对(Oracle 族对 MySQL 族);同族比对(OB-Oracle 对 Oracle、OB-MySQL 对
MySQL)不受影响。

### 6.3 Oracle `DATE` 修正的兼容性

已存在的 Oracle 比对任务(Preview 期)若改了映射,同一张表前后两次跑出的结果可能变化(以前
漏报的时间差异会被报出来)。这是修 bug,不做开关;在发布说明里写明。

---

## 7. SQL 工作台、导出、差异 SQL、行数限制

- `sql_build` / `result_export` 改走 `traits().identifier_style`(§3.2 PR-0 完成后
  OB-MySQL 自动正确)。
- `query_limit` / `compare_sql` 的 `_DIALECTS` 改读 `traits().sqlglot`。
- 11g:`limit_clause` 对 `rownum` 风格生成
  `SELECT * FROM (<sql>) WHERE ROWNUM <= n`;`apply_database_row_limit` 用 sqlglot 的
  oracle 方言同样能生成,单测覆盖。
- 前端:Oracle 放开执行/工具/自动补全,通过 §3.3 的 `capabilities` 达成。按仓库规矩,
  “Preview 档放开执行”这一步是独立 PR,由你拍板。
- 自动补全的关键字表:`sqlIntelligence.ts` 按 `family` 选 MySQL 或 Oracle 关键字集,
  OB 两种模式分别归入。

---

## 8. 血缘与 AI

- `lineage/dialects.py`、`ai_upstream_locator.py:222`、`sql_assistant.py:277`、
  `api/routes/core.py:8566` 改读 `traits().lineage` / `traits().sqlglot`:
  `oceanbase_mysql → mysql`,`oceanbase_oracle → oracle`。
- 血缘方言下拉(`LineageView.vue:87/788`)**不新增 OB 选项**——血缘解析只关心 SQL 方言,
  OB 两种模式与 mysql/oracle 解析规则相同;按数据源自动带方言时走映射即可。
- OB 专有语法(如 MySQL 模式下的 `PARTITION BY ... ` 扩展、Hint 写法)若 sqlglot 解析失败,
  走现有 `ai_fallback`;P4 用真实存过程/脚本样本评估失败率,超过阈值再考虑写 sqlglot 方言
  扩展。

---

## 9. 打包与依赖

| 项 | 结论 |
|---|---|
| oracledb | 已锁定 4.0.1,已在三类包中,不变 |
| PyMySQL | 已有,方案 A 不加依赖 |
| pyoceanbase | 仅方案 B 才加;锁版本 + 哈希,进主依赖(纯 Python,体积可忽略) |
| Instant Client | 不进包(体积门槛 + 许可),部署方放置,`DATAOPS_ORACLE_CLIENT_DIR` 开 thick |
| 包体基线 | Win10 GUI / Mint 都是“只许变小”。方案 A 零增长;方案 B 需实测增量,若超基线需你批准调基线 |

---

## 10. 安全红线对照

| 红线 | 本设计的落点 |
|---|---|
| R1 驱动只在 `app/dbclients/` import | `oceanbase_oracle_driver.py`、新适配器、会话缝都在 `app/dbclients/`;`pyoceanbase`(若采用)加入 ruff banned-api 清单 |
| R2 只用 SecretRef,无明文 | 密码仍只经 `SecretStore.reveal_secret` 进驱动 kwarg;`extra` 白名单拒绝未知键,防止口令落进 `capability_profile`;OB 用户名拼装不含口令 |
| R5 日志无敏感值 | 新错误分类器复用 `_sanitize_error_text`;OB 用户名含租户/集群名,按“连接信息”对待,不进日志 |
| 测试凭据 | 真库测试只读环境变量(§11.2),不写入仓库 |

---

## 11. 测试与验收

### 11.1 单元测试(CI 跑,不连库)

- `traits()` 全类型快照;PR-0 前后 `sql_build` / `result_export` / `query_limit` 输出逐字节对照。
- 各新适配器:用假驱动模块注入(现有 `pymysql_module=` / `oracledb_module=` 注入点同款),
  覆盖用户名拼装、超时 SQL、系统库过滤、错误分类、`capabilities`。
- `oracle_compare`:SQL 文本快照 + 降级原因(超长载荷、CLOB、11g 无授权)。
- `oceanbase_oracle_driver.escape_literal`:反斜杠、单引号、`%`、中文。
- `oceanbase_oracle_types`:扩展类型码映射与文本解码(用 POC 抓到的真实样本做夹具)。
- 前端:`capabilities` 驱动的按钮显隐;老后端无字段时回落。

### 11.2 真库集成测试(环境变量门控,`pytest.mark.integration`)

沿用 `DATAOPS_TEST_MYSQL_*` / `DATAOPS_TEST_DM_*` 的命名:

```text
DATAOPS_TEST_ORACLE_HOST / _PORT / _USERNAME / _PASSWORD / _SERVICE_NAME
DATAOPS_TEST_OBMYSQL_HOST / _PORT / _USERNAME / _PASSWORD / _DATABASE / _TENANT / _CLUSTER
DATAOPS_TEST_OBORACLE_HOST / _PORT / _USERNAME / _PASSWORD / _SCHEMA / _TENANT / _CLUSTER
```

新增或扩展的测试文件:

| 文件 | 内容 |
|---|---|
| `test_oracle_real_instance.py`(新) | 对照 `test_dm_real_instance.py`:连接、元数据、DDL、explain、超时、软取消 |
| `test_oceanbase_real_instance.py`(新) | 两种模式各一组,同上 |
| `test_session_broker_oracle.py` / `_oceanbase.py`(新) | 对照 `test_session_broker_mysql.py`:硬取消、超时确认码、连接死亡 |
| `test_compare_golden_databases.py`(扩) | 黄金表同一份数据灌进各库,逐行哈希与段聚合哈希**两两相等** |
| `test_compare_run_golden_databases.py`(扩) | 端到端比对作业,覆盖 §6.2 的每一类差异 |
| `test_lineage_real_databases.py`(扩) | OB 两种模式的样本 SQL |

黄金比对矩阵(至少):

| 源 \ 目标 | MySQL | OB-MySQL | Oracle | OB-Oracle | DM |
|---|---|---|---|---|---|
| OB-Oracle | ✔ | ✔ | ✔ | — | ✔ |
| OB-MySQL | ✔ | — | ✔ | ✔ | — |
| Oracle | ✔ | ✔ | — | ✔ | ✔ |

最终组合按你给的业务优先级裁剪。

### 11.3 Certified 验收文档

每种方言一份,对照 `docs/acceptance-dm-certified.md` 与 PR #154 列的 Oracle Certified 前置项:

- `docs/acceptance-oracle-certified.md`
- `docs/acceptance-oceanbase-mysql-certified.md`
- `docs/acceptance-oceanbase-oracle-certified.md`

内容:验证方式、真机暴露并修复的问题、环境注记、口径升级。前端相关结论必须有浏览器证据(控
制台无报错、连真实后端),按仓库规矩。

---

## 12. 分阶段与 PR 切分

每个 PR 单独走 feat 分支 → PR → 人工评审 → 人工 squash。

| PR | 阶段 | 内容 | 依赖 | 规模 |
|---|---|---|---|---|
| PR-0 | 重构 | `app/domain/dialect.py` + §2.2 后端硬分支收敛;worker 改读 `compare_db_hash`;行为零变化 | — | M |
| PR-1 | 重构 | `DatasourceResponse.capabilities` + 前端改读能力(保留回落) | PR-0 | M |
| PR-2 | P0 | POC 脚本(`tools/poc/db_probe.py`,不进运行时),逐项跑 §13;产出 POC 报告并回改本稿 | 实例 | S |
| PR-3 | P1 | Oracle:`DATE` 映射修正、explain、DDL、错误分类、`oracle_catalog` 抽取 | PR-0 | M |
| PR-4 | P1 | Oracle:`oracle_compare`(从 `dm_compare` 抽公共)+ 哈希下推 + 黄金测试 | PR-3 | L |
| PR-5 | P1 | Oracle:会话控制台(`local_cancel` 协议扩展 + Oracle 缝) | PR-3 | M |
| PR-6 | P1 | Oracle:前端放开执行/工具(`PREVIEW_EXECUTION_ALLOWED` 或升 Certified)——**需你单独拍板** | PR-1、PR-3 | S |
| PR-7 | P2 | OB-MySQL:新类型 + 适配器 + 表单字段 + 会话缝 + 哈希下推验证 | PR-0、PR-1、PR-2 | M |
| PR-8 | P3 | OB-Oracle:驱动模块 + 类型解码 + 适配器 | PR-2、PR-3 | L |
| PR-9 | P3 | OB-Oracle:哈希下推 + 会话缝 | PR-4、PR-8 | M |
| PR-10 | P4 | 跨库黄金矩阵、`rtrim_char` 规则(若批准)、三份 Certified 验收文档 | 全部 | M |

可并行:PR-0 完成后,PR-3 与 PR-7 互不相干;PR-1 与 PR-3 也可并行。PR-2(POC)只等实例。

**在拿到实例之前能先做的**:PR-0、PR-1,以及 PR-3/PR-4 中不依赖真库的部分(单测快照)。

---

## 13. 待 POC 验证清单

| 编号 | 问题 | 影响 |
|---|---|---|
| V1 | 各版本的版本串/`ob_version()` 输出;能否据此区分 3.x/4.x 与模式 | 测试连接、存量类型提示 |
| V2 | OB-Oracle 扩展类型码与文本格式 | §4.3.4 |
| V3 | 三种方言的取消、超时、登录失败、连接断开的数值错误码 | 错误分类器 |
| V4 | PyMySQL 在 OB-Oracle 下的转义行为、`NO_BACKSLASH_ESCAPES` 状态位 | §4.3.3 |
| V5 | `ob_query_timeout` 在两种模式下的设置方式与生效范围;`MAX_EXECUTION_TIME` 是否支持 | 超时 |
| V6 | OB 两种模式的系统库/系统 schema 名单;OB-Oracle `ALL_*` 视图的列差异 | 元数据 |
| V7 | OB-Oracle 是否支持 `FETCH FIRST n ROWS ONLY` | 分页 |
| V8 | OBProxy 对流式结果集的空闲超时 | 大结果集、比对 |
| V9 | OB 两种模式取 DDL 的方式 | DDL |
| V10 | OB-Oracle 的 `EXPLAIN PLAN FOR` / `DBMS_XPLAN` | 执行计划 |
| V11 | OB-MySQL 上 MySQL 哈希表达式的输出是否与 MySQL 逐字节一致;OB-Oracle 上可用的 MD5 函数 | 哈希下推 |
| V12 | 经 OBProxy 的 `KILL QUERY` 路由;OB-Oracle 的会话标识与 `KILL` 语法 | 会话控制台硬取消 |
| V13 | 方案 A(PyMySQL 置 bit 27)能否连上 OB-Oracle;不能则试方案 B | 驱动选型 |

---

## 14. 待你拍板的事项

1. **OB-Oracle 驱动**:同意先 POC 方案 A、失败再 B 吗?采用 B 即新增依赖。
2. **11g**:现场有没有 11g。有的话才实现 thick 开关(Instant Client 由部署方放置)。
3. **新增 `rtrim_char` 比对规则**:改规范与三方实现,还是先只靠 `trim_strings`。
4. **Oracle 前端放开执行**(PR-6):在升 Certified 之前,是否先以 Preview 档放开。
5. **PR-0 / PR-1 先行**:实例到位前先做这两个纯重构,可以吗。
6. **测试实例**(评估时列过):Oracle 一套;OB 企业版两个租户;开发机能否直连;只读账号 +
   可建表 schema;要核的组合与优先模块。

---

## 附录 A:参考资料

- OceanBase Python 连接 Oracle 模式(cx_Oracle + OBCI):
  <https://en.oceanbase.com/docs/common-oceanbase-database-10000000003453840>
- pyoceanbase:<https://pypi.org/project/pyoceanbase/>
- python-oracledb 文档:thin/thick 模式、`init_oracle_client`、`Connection.cancel()`
- Oracle `STANDARD_HASH`(12.1 起)、`DBMS_METADATA`、`DBMS_XPLAN` 官方文档
- 仓库内:`docs/design/compare-normalization-spec.md`、`docs/acceptance-dm-certified.md`、
  `docs/adr/0023-console-session-broker.md`、PR #154(Oracle Certified 前置项)
