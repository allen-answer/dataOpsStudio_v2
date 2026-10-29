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
  - ~~若现场没有 11g,本开关不实现~~。**现场有 11g,开关必须实现**(见下方 POC 结论)。
- 版本探测:`test_connection` 现已读 `product_component_version`,结果额外缓存主版本号,
  供分页语法(11g 用 `ROWNUM` 包裹)和哈希函数选型(§6.1.2)使用。

> **11g POC 结论(2026-10-27,Oracle 11.2.0.4.0 EE,库字符集 ZHS16GBK,只读账号,全程只读)**
>
> - thin 模式握手即报 `DPY-3010`(在认证之前,用不存在的用户名即可探测,不会给业务账号
>   记失败登录)。thick 模式 + Instant Client **19.29 basiclite**(约 39 MB)连接正常;19c 是
>   支持 11.2 的最高客户端版本,README 里写死“用 19c”。thin 不可用时 `test_connection`
>   把 `DPY-3010` 翻译成“该库低于 12.1,需部署方配置 `DATAOPS_ORACLE_CLIENT_DIR`”。
> - 账号权限只有 `CREATE SESSION` + `CONNECT` 角色:`V$PARAMETER`、`V$SQL`、`V$SESSION`
>   都读不到(942)。所有能力的设计都要按**这种最小权限账号**成立,读 V$ 的一律可选。
> - `ALL_*` 目录视图(`ALL_TABLES/ALL_TAB_COLUMNS/ALL_TAB_COMMENTS/ALL_COL_COMMENTS/
>   ALL_CONSTRAINTS/ALL_IND_COLUMNS/ALL_VIEWS/ALL_TAB_PARTITIONS/ALL_USERS`)都可读;
>   11g 的 `COLUMN_ID` 从 1 起编(与 OB 的 16 不同)。业务库里出现了 `DATA_TYPE='UNDEFINED'`
>   的列,类型映射必须落 `UNKNOWN` 而不是报错。
> - 分页:`FETCH FIRST` 报 `ORA-00933`;只能用两层 `ROWNUM` 包裹(`ROWNUM <= hi` 在内、
>   `rn > lo` 在外),实测正确。
> - `DBMS_METADATA.GET_DDL` 取**他人 schema** 的表报 `ORA-31603`(缺 `SELECT_CATALOG_ROLE`),
>   与 §4.1.2 的预判一致:返回结构化的 `ddl_permission_denied`。
> - 执行计划:`PLAN_TABLE` 是 `PUBLIC` 同义词 → `SYS.PLAN_TABLE$`(会话级临时表),
>   `DBMS_XPLAN` 对 PUBLIC 可执行;`DISPLAY_CURSOR` 因读不到 `V$SESSION` 不可用。
>   `EXPLAIN PLAN` 会向会话临时表写行,**本次按“只读”约定未执行**,能否在只读账号下跑通
>   留待现场确认;闸门 `allow_oracle_plan_table` 缺省 False 不变。
> - **取消**:另一线程调 `Connection.cancel()` 实测得到 `ORA-01013`,连接**仍可继续用**;
>   但从发出取消到调用返回用了约 10 秒(CPU 密集的笛卡尔积查询,经两级隧道),UI 文案不能
>   承诺“立即停止”。
> - **`call_timeout` 会毁掉连接**:thick 模式下到时报 `DPY-4011`,之后该连接 `DPY-1001`
>   不可再用。现有 `_apply_statement_timeout`(`oracle_adapter.py:502`)把它当语句超时用,
>   这意味着每次超时都要重建连接;会话控制台里尤其不能用它做常规超时,只能作最后的保险带,
>   常规超时改为 broker 计时 + `local_cancel()`。
> - **NUMBER 精度丢失(现有 bug,与 11g 无关)**:oracledb 缺省把 NUMBER 取成 `float`,
>   实测 `NUMBER(20,2)` 的 `12345678901234567.89` 取回 `1.2345678901234568e+16`。现有
>   适配器没有设 `fetch_decimals` 或输出类型处理器,SQL 工作台展示、导出、比对的客户端
>   哈希都会受影响。修法:每个连接设 `outputtypehandler`,NUMBER 一律取 `Decimal`(不动
>   进程级 `oracledb.defaults`,避免牵连其他模块);同时 CLOB/NCLOB 按 `str`、BLOB 按
>   `bytes` 直接取(缺省是 LOB 对象,需再次往返)。这条单独拆成 PR-0.5 先修。

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
  等)。11g 实测:`942` 表不存在、`900` 语法错、`933` 语法未结束、`1476` 除零、`1017` 登录
  失败、`1013` 取消、`31603` 无 DDL 权限、`28231` MD5 输入为空、`6502` RAW 超长;thick 模式
  `call_timeout` 到时报 `DPY-4011`(连接已断)而不是 `DPY-4024`,分类器两者都要认。
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

> **POC 结论(2026-10-27,OB 4.3.5.4 企业版,经 OBProxy)**:方案 A 成立。不置 bit 27 登录
> 报 `1235 Oracle tenant for current client driver is not supported`,置 `client_flag=1<<27`
> 后正常登录、查询。读过 pyoceanbase 1.1.0 源码:与 PyMySQL 的差别只有这一位和若干上游
> PyMySQL 的小修,**对扩展类型码、转义都没做任何处理**,不采用 B。

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
| 测试连接 | `SELECT 1 FROM DUAL`;版本 `SELECT ob_version() FROM DUAL`(实测 `4.3.5.4`;`VERSION()` 在 Oracle 模式**不存在**)。经 OBProxy 时握手里的 `server_version` 是代理的 `5.6.25`,不含 OceanBase 字样,**不能据握手判断**;模式用 `SHOW VARIABLES LIKE 'ob_compatibility_mode'`(两种模式都可用,返回 `ORACLE`/`MYSQL`)核对,填错类型时测试连接给出明确提示 |
| 元数据 | `oracle_catalog` 的 `pyformat` 渲染;`ALL_TABLES/ALL_TAB_COLUMNS/ALL_CONSTRAINTS/ALL_CONS_COLUMNS/ALL_INDEXES/ALL_IND_COLUMNS/ALL_VIEWS/ALL_TAB_COMMENTS/ALL_COL_COMMENTS/ALL_OBJECTS/ALL_SYNONYMS/ALL_SEQUENCES/ALL_TAB_PARTITIONS/ALL_SOURCE` 实测都在,列名与 Oracle 一致。`COLUMN_ID` 从 16 起编(OB 内部列号),只能用于排序,不能当序号;`DATA_TYPE` 带精度(`TIMESTAMP(6)`),与 Oracle 相同。`SESSION_PRIVS`/`SESSION_ROLES` **不存在**,权限探测改用 `USER_TAB_PRIVS` 或直接试执行。系统 schema 排除 `SYS`/`LBACSYS`/`ORAAUDITOR`/`PUBLIC` |
| 语句超时 | 实测 `SET ob_query_timeout = <微秒>`、`ALTER SESSION SET ob_query_timeout = ...`、hint `/*+ query_timeout(<微秒>) */` 三种都生效;超时报 `600` + 内部码 `-4012`。`MAX_EXECUTION_TIME` hint 不报错但**无效果**,不用。会话缺省值 604800 秒(7 天),必须显式设 |
| 流式读 | PyMySQL `SSCursor`(OBProxy 空闲行为见 V8 结论) |
| `explain` | `EXPLAIN <sql>` 返回单列 `Query Plan` 文本行;`EXPLAIN FORMAT=JSON <sql>` 返回逐行 JSON 文本。**`EXPLAIN PLAN FOR` 语法报错**,`DBMS_XPLAN.DISPLAY` 因无 `PLAN_TABLE` 报 942,二者都不用 |
| DDL | `DBMS_METADATA.GET_DDL('TABLE'/'VIEW', name, owner)` 实测可用,表 DDL 尾部带 OB 专有子句(`COMPRESS FOR ARCHIVE REPLICA_NUM = 3 BLOCK_SIZE = ...`),原样返回;`SHOW CREATE TABLE owner.t` 输出相同,作备选。系统视图(`SYS.ALL_OBJECTS` 等)取 DDL 报 31603,前端只对用户对象提供 |
| 哈希下推 | 见 §6.1.1:只有 `DBMS_CRYPTO.HASH`,且慢 |
| 分页 | 实测 `OFFSET m ROWS FETCH NEXT n ROWS ONLY`、`FETCH FIRST n ROWS ONLY`、`ROWNUM` 都可用;`LIMIT` 语法报错。用 `FETCH FIRST` |

#### 4.3.3 绑定参数转义(必须处理的坑)

PyMySQL 的参数是**客户端插值**:`%s` 被替换成转义后的字面量。它的字符串转义默认按 MySQL
规则把 `\` 变成 `\\`;只有服务端在握手状态里报告 `NO_BACKSLASH_ESCAPES` 时才只做 `'`→`''`。
Oracle 方言里反斜杠不是转义符,如果 OB-Oracle 不报这个状态位,**含反斜杠的绑定值会被改写**
(比对的分段边界、元数据查询的 owner 名都会受影响)。

处理:`oceanbase_oracle_driver.escape_literal` 固定按 Oracle 规则转义(只双写单引号),连
接建立后把 PyMySQL 连接的 `escape_string` 换成它。

> **POC 结论(V4)**:坑是真的。服务端 `server_status` 里**不报** `NO_BACKSLASH_ESCAPES`
> (`0x200` 位为 0),但 SQL 层把反斜杠当普通字符(`length('a\b') = 3`,`'a\'` 是合法字面量)。
> PyMySQL 缺省把 `a'b\c` 转成 `'a\'b\\c'`,服务端直接报 900 语法错——即**所有含单引号的绑定值
> 都会失败**,不只是反斜杠。按 Oracle 规则 `'a''b\c'` 往返正确(长度 5)。

#### 4.3.4 扩展类型解码

POC(V2)在 `charset=utf8mb4` 与 `gbk` 两种连接字符集下逐类型抓了 `description` 与原始字节,
结论如下(OB 4.3.5.4):

| Oracle 类型 | 类型码 | 线上格式(文本协议) | 解码 |
|---|---|---|---|
| `NUMBER(p,s)` / `NUMBER` | 246 | 十进制文本 | PyMySQL 已解成 `Decimal`;`scale` 从 description 读 |
| `FLOAT(b)` | 206 | 十进制文本 | 自解为 `Decimal` |
| `BINARY_FLOAT` / `BINARY_DOUBLE` | 4 / 5 | 文本 | PyMySQL 已解成 `float` |
| `VARCHAR2` / `CHAR` | 253 / 254 | 连接字符集文本(charsetnr 46/87) | PyMySQL 已解;`CHAR` **带补齐空格** |
| `NVARCHAR2` / `NCHAR` | 207 / 208 | 连接字符集文本 | **必须自解**(见下) |
| `DATE` | 12 | `YYYY-MM-DD HH24:MI:SS` 文本 | PyMySQL 已解成 `datetime`,时间部分保留 |
| `TIMESTAMP(n)` | 202 | **二进制** 12 字节:世纪、年、月、日、时、分、秒各 1 字节,纳秒 4 字节小端,精度 1 字节 | 自解为 `datetime`(微秒截断,纳秒另存供比对) |
| `TIMESTAMP WITH TIME ZONE` | 200 | 同上 12 字节 + 时区时(有符号 1 字节)+ 时区分(1 字节,符号随时)+ 区域名长度 1 字节 + 区域名 + 缩写长度 1 字节 + 缩写 | 自解为带 `tzinfo` 的 `datetime`;区域名(`Asia/Shanghai`)保留 |
| `TIMESTAMP WITH LOCAL TIME ZONE` | 201 | 同 202,已换算到会话时区 | 自解;会话时区实测 `+08:00` |
| `RAW` | 203 | 原始字节(charsetnr 63) | 返回 `bytes` |
| `INTERVAL YEAR TO MONTH` | 204 | 符号 1 字节 + 年 4 字节小端 + 月 1 字节 + 年精度 1 字节 | 自解为规范文本 `-012-11`(与 `TO_CHAR` 一致) |
| `INTERVAL DAY TO SECOND` | 205 | 符号 1 + 天 4 小端 + 时/分/秒各 1 + 纳秒 4 小端 + 天精度 1 + 秒精度 1 | 自解为 `timedelta` + 规范文本 |
| `CLOB` / `BLOB` | 251 | 文本(charsetnr 46/87)/ 字节(63) | PyMySQL 已解(`str` / `bytes`) |
| `NULL` 字面量 | 6 | — | `None` |

以上解码器在 POC 里与同一值的 `TO_CHAR(...)` 逐项对过,包括 0001 年、9999 年、负时区
`-03:30`、区域名时区、负 INTERVAL。未遇到 209–211(UROWID 等;`ROWID` 伪列对 `DUAL` 不可用,
`TO_NCLOB` 函数不存在),留待有真实列时补。

**必须接管 PyMySQL 的字段解码,不能只注册 converter**:PyMySQL 在 `MySQLResult._get_descriptions`
里对不在 `TEXT_TYPES` 里的类型码一律按 `ascii` 解码,然后才交给 converter。实测后果:

- `NVARCHAR2` 含中文 → `UnicodeDecodeError`,且**异常发生在读包中途,连接随即包序号错乱
  (`Packet sequence number wrong`),后续所有语句都失败**;
- 200–202/205 的二进制值里只要有 ≥0x80 的字节(纳秒字段几乎必然有)就同样崩。

做法:`oceanbase_oracle_driver` 提供 `Connection` 子类,覆盖 `_read_query_result` 使用自己的
`MySQLResult` 子类,在 `_get_descriptions` 里按类型码决定每列编码:207/208 用连接编码,
200–205 置 `None`(拿原始字节),206 按文本;再由 `oceanbase_oracle_types` 的解码器转值。
**不改** `pymysql.connections.TEXT_TYPES` 这类模块级全局(会波及同进程的 MySQL 连接)。
子类依赖 PyMySQL 私有方法,锁死 PyMySQL 版本,并用单测快照字段解码路径。

连接字符集:库字符集是 `ZHS16GBK`,连接用 `utf8mb4` 时服务端返回 UTF-8 字节(charsetnr 46),
中文往返正确。固定用 `utf8mb4`,不随库字符集变。

类型码 → `ColumnType` 映射放 `app/dbclients/oceanbase_oracle_types.py`,与 `oracle_types` 的
口径对齐(`DATE → DATETIME`,NUMBER 的“scale==0 归 INTEGER”规则沿用)。

---

## 5. 会话控制台(Session Broker)

现状只有 MySQL/DM(`app/broker/wiring.py:35`)。三种新方言各加一对“会话连接 + 控制通道”,
放进 `app/dbclients/interactive/`,接口不变(`BaseInteractiveConnection` /
`BaseCancelChannel`)。

| 方言 | 会话标识(marker) | 硬取消 | 破坏性兜底 | 服务端语句超时 | 取消/超时确认码 |
|---|---|---|---|---|---|
| OB-MySQL | `SELECT CONNECTION_ID()` | `KILL QUERY <id>` | `KILL <id>` | `ob_query_timeout`(微秒) | 1317 取消;OB 超时码(4012 族)〔待验证 V3〕 |
| OB-Oracle | `SELECT SYS_CONTEXT('USERENV','SID') FROM DUAL`(实测与 PyMySQL `thread_id()`、`GV$OB_PROCESSLIST.ID` 是同一个数;`CONNECTION_ID()` 不存在) | `KILL QUERY <id>`(实测可用,经 OBProxy,约 1 秒内打断,被打断连接**仍可继续用**) | `KILL <id>`(实测普通账号可用,目标连接收到 2013);`ALTER SYSTEM KILL SESSION` 报 1031 无权限,不用 | `ob_query_timeout` | 取消 `600`/内部码 `-5065`;超时 `600`/`-4012` |
| Oracle | `DBMS_DEBUG_JDWP.CURRENT_SESSION_ID/_SERIAL` 拼 `sid,serial#`(`V$SESSION` 最小权限不可读;JDWP 被收回时退回只有 sid) | **进程内** `Connection.cancel()`(见下) | `ALTER SYSTEM KILL SESSION 'sid,serial#' IMMEDIATE`,需权限,无权时执行即失败 | 无语句级服务端超时;`call_timeout` 作保险带 | ORA-01013 取消;DPY-4024 超时(按 `full_code` 分类) |

**Oracle 的取消走进程内,不走控制连接 SQL**:

- `ALTER SYSTEM CANCEL SQL` 要 18c+ 且需 `ALTER SYSTEM` 权限,生产只读账号基本没有。
- python-oracledb 的 `Connection.cancel()` 正是设计成**由另一线程**打断本连接正在执行的调用,
  不要任何权限。会话连接与控制 lane 同在 API 进程内,条件天然满足。
- 协议改动:`InteractiveConnection` 增加可选方法 `local_cancel() -> bool`,Oracle 实现调
  `conn.cancel()`,其他方言返回 False。broker 的取消阶梯在“硬取消”一级先试
  `local_cancel()`,不行再走 `CancelChannel.cancel(marker)`。MySQL/DM 行为不变。
- `OracleCancelChannel` 只负责破坏性兜底(`KILL SESSION`)。**PR-5 定稿**:控制通道恒报
  `AVAILABLE` —— 硬取消不经控制连接、不需权限,报 `DEGRADED` 会让前端误提示“取消将断开会话”;
  兜底无权限时在执行时失败,broker 记 `destroy_failed`(前端文案区分留作后续)。

**PR-5 实测**(11.2.0.4,仅 CONNECT 角色,thick):`Connection.cancel()` 约 2 秒打断笛卡尔积
长查询,报 ORA-01013,连接照常续用;真 `SessionBroker`(内存 store)上取消到语句
`cancelled:1013` 约 0.2 秒,下一条语句成功。`call_timeout` 超时报 DPY-4024,其 `.code` 为 0,
分类器按 `full_code` 查表。另:broker 的安全限行按 Oracle 方言渲染 `FETCH FIRST`,11g 不认,
会话缝连到 12c 以前的库时与 job 路径一样改写为 ROWNUM(带 OFFSET 时去掉行号列)。
thick 初始化是进程级的,job 路径与会话缝共用 `oracle_connect` 里同一份初始化状态。

OB 经 OBProxy 时,会话号是代理侧会话号,`KILL QUERY` 由代理路由到后端 observer。OB-Oracle
在 proxy 模式下已实测可用;OB-MySQL 尚无实例,按同一机制预期可用,待验证。

错误分类器:`OceanBaseMySQLErrorClassifier`(继承 MySQL 码表再补 OB 码)、
`OceanBaseOracleErrorClassifier`、`OracleErrorClassifier`(与 §4.1.3 共用码表)。原则是**只按
数值码分类,不看消息文本**(`interactive/protocol.py` 的既定原则)。

OB-Oracle 实测码(V3):`942` 表不存在、`900` 语法错、`1476` 除零、`1722` 无效数字、`1489`
拼接过长、`1031` 无权限、`1045` 登录失败(密码错与租户不存在**同码**,消息里不区分)、`2013`
连接被断(客户端码)。**超时、取消、函数不存在都报同一个 `600`**,区分它们的内部码只出现在
消息的固定前缀 `OBE-00600: internal error code, arguments: -<n>` 里。只看外层码就无法区分
“用户取消”和“语句超时”。

处理:分类器对 `600` 一码放行一条**锚定前缀**的解析(只匹配 `^OBE-00600: internal error code,
arguments: (-\d+)`,取出数值再查码表:`-4012` 超时、`-5065` 取消、`-5055` 对象不存在),
其余情况仍只看数值码。这算对既定原则的一个窄口子,需要在 `protocol.py` 的注释里写明理由;
你如果不接受,备选是 `600` 一律归“服务端错误”,取消/超时靠 broker 自己的状态判断。

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
| Oracle | 十六进制串 `\|\|`(见 §6.1.5) | 探测 `STANDARD_HASH`(12.1+)→ `DBMS_CRYPTO.HASH` → **11g `DBMS_OBFUSCATION_TOOLKIT.MD5(input => HEXTORAW(载荷))`** | 复用 DM 的 `hex_to_uint64_expression`(拆半 `TO_NUMBER`) | `oracle_compare`(PR-4 已实现) |

**11g 哈希实测**(11.2.0.4,最小权限账号):`STANDARD_HASH` 不存在(904);`DBMS_CRYPTO` 未授权
(904);**`DBMS_OBFUSCATION_TOOLKIT` 对 PUBLIC 可执行**,`MD5` 结果与 Python 逐字节一致,
取前 16 个十六进制位转 uint64 也一致。约束:

- 输入 RAW 在 SQL 上下文里**上限 2000 字节**(2000 通过,2001 报 `6502`),所以 11g 的载荷
  上限按 2000 字节估算,超过即降级客户端哈希(比 §6.1.2 的 4000 更紧);
- 输入为空报 `ORA-28231`,载荷构造必须保证非空(规范的分隔符/长度前缀已保证,单测锁住);
- GBK 库必须 `CONVERT(x,'AL32UTF8')`,实测后与 UTF-8 MD5 一致;
- 速度:10 万行约 1 秒(`ORA_HASH` 约 0.15 秒),可用。该包在 Oracle 文档里标为已弃用,
  但 11g 上是唯一免授权的 MD5;12c+ 一律用 `STANDARD_HASH`,不走它。
- 探测顺序(§6.1.3):`STANDARD_HASH` → `DBMS_CRYPTO` → `DBMS_OBFUSCATION_TOOLKIT` → 客户端。
| OB-Oracle | `\|\|` | **只有** `DBMS_CRYPTO.HASH(UTL_RAW.CAST_TO_RAW(CONVERT(x,'AL32UTF8')), 2)`(实测 `STANDARD_HASH`、`MD5()` 均不存在) | `TO_NUMBER(SUBSTR(RAWTOHEX(h),1,16),'XXXXXXXXXXXXXXXX')`,实测与 Python 端 uint64 一致 | 新 `oracle_compare`(variant) |

**OB-Oracle 哈希下推的两条实测结论**(V11):

1. **字符集**:库字符集 `ZHS16GBK` 时,`UTL_RAW.CAST_TO_RAW('中文')` 得到的是 GBK 字节
   `D6D0CEC4`,MD5 与 UTF-8 侧不同。必须先 `CONVERT(x,'AL32UTF8')`(或
   `UTL_I18N.STRING_TO_RAW(x,'AL32UTF8')`),实测后与 Python `md5('中文'.encode('utf-8'))`
   一致。这条同样适用于 GBK 字符集的 Oracle,并替代 §6.1.2 里“非 UTF-8 规范化字符集即降级”
   的保守规则(Oracle 族可以库内转码)。
2. **性能**:`DBMS_CRYPTO.HASH` 是 PL/SQL 包函数,逐行调用。在 `CONNECT BY` 生成的行上实测
   约 **1 万行/秒**(20 万行 22 秒,100 万行 105 秒),同样数据 `ORA_HASH` 约 20 万行/秒。
   规范禁用 `ORA_HASH`,所以库内下推在 OB-Oracle 上**可能比把行拉回客户端算还慢**。
   设计改为:OB-Oracle 缺省 `CLIENT_ROW_HASH`,库内下推做成能力但默认关闭,待有真实大表时
   对比两条路径(含网络)再决定缺省值。这一项列入 §14 待决。

`oracle_compare.py` 只复用 `dm_compare.hex_to_uint64_expression`,载荷构造独立实现:Oracle 的
字符集与 `''` 即 NULL 两点让 DM 那套拼接没法直接用(§6.1.5),DM 的输出 SQL 不动。

#### 6.1.5 PR-4 实测与定稿(11.2.0.4,ZHS16GBK / AL16UTF16)

上面 11g 行里的 `CONVERT(x,'AL32UTF8')` 写法**在 GBK 库上不成立**,实测推翻:

- 缺省哨兵 U+2400、分隔符 U+241F 不在 GBK 里,写成 VARCHAR2 字面量即变 `?`;
- `UTL_I18N.STRING_TO_RAW(nvarchar2, 'AL32UTF8')` 在 SQL 里先经库字符集,U+2400 得到
  `EFBC9F`(全角问号),GBK 外字符一律丢失。

定稿写法:**载荷按 UTF-8 十六进制串拼接**,SQL 全程只有 ASCII。

- 固定字面量(NULL 位、哨兵、分隔符、哨兵长度)在 Python 侧算好 UTF-8 十六进制;
- 文本值 `TO_NCHAR` 后取国家字符集字节,`UTL_RAW.CONVERT(UTL_RAW.CAST_TO_RAW(v),
  'AMERICAN_AMERICA.AL32UTF8', 'AMERICAN_AMERICA.<NLS_NCHAR_CHARACTERSET>')` 转 UTF-8,
  不经库字符集(GBK 外 BMP 字符、增补平面字符均与 Python 逐字节一致);`CONVERT(NULL)` 报
  ORA-06502,值为 NULL 时先短路;
- 数值、日期只含 ASCII,`UTL_RAW.CAST_TO_RAW` 原样取字节;
- 最后 `HEXTORAW` 一次喂给 MD5。

与客户端口径对齐的几处:

| 差异 | 处理 |
|---|---|
| TRIM 全空格得 NULL,客户端得 `''` | NULL 位不开 `empty_as_null` 时只看原列;值为 NULL 按空串拼、长度记 0 |
| `LENGTH` 对 NVARCHAR2 数 UTF-16 码元(😀=2) | 用 `LENGTHC`(码点) |
| `CAST(ts AS TIMESTAMP(p))` 四舍五入,客户端截断 | `TO_CHAR(CAST(x AS TIMESTAMP(9)), '...FF9')` 后 `SUBSTR` |
| BINARY_DOUBLE 转 NUMBER 得二进制精确值(2.675 → 2.67499…),客户端按最短往返 `2.675` 舍入 | 依次试 15/16/17 位有效数字,取第一个能转回原值的;需 `driver_type` 区分 BINARY_* 与 NUMBER 型 FLOAT,缺失即降级 |

降级(返回 `CLIENT_ROW_HASH` 并写原因):无可用 MD5、国家字符集不是 AL16UTF16/UTF8、BYTES /
BOOLEAN / UNKNOWN 列、`driver_type` 为 CLOB/NCLOB/LONG/XMLTYPE、FLOAT 无 `driver_type`、规范化字符集
非 UTF-8。载荷上限 2000 字节(十六进制串受 VARCHAR2 4000 限制),超长行执行时报 ORA-01489,
worker 退回客户端哈希并记日志。数据源 `extra.compare_md5_function` 可限定为某个函数或 `client`。

真库对照(只读,内联视图 + `SYS.ALL_OBJECTS`):六组规则 × 边界数据逐行一致,`ALL_OBJECTS`
3000 行一致;10 万行单段聚合库内 11.3 秒,客户端取数加哈希 24.8 秒(经 VPN)。11g 的
`DBMS_OBFUSCATION_TOOLKIT` 约 9 千行/秒,大表下推是否划算仍要在现场网络上比。

OB-Oracle 没有 `UTL_RAW.CONVERT`(实测 `-5055 FUNCTION UTL_RAW.CONVERT does not exist`),
`DBMS_CRYPTO.HASH(UTL_I18N.STRING_TO_RAW(...))` 遇 GBK 外字符报 `-4258 Incorrect string value`,
这套写法不能照搬,留给 PR-9。

#### 6.1.2 Oracle 特有降级原因

- **载荷超长**:按列元数据估算载荷最大字节数(列 `CHAR_LENGTH`/`DATA_LENGTH` × 字符集
  最大字节数 + 分隔符 + 长度前缀),超过 4000(`MAX_STRING_SIZE=STANDARD`)即降级客户端哈希,
  原因写 `oracle payload may exceed VARCHAR2 limit`。适配器在首次构造计划时读一次
  `V$PARAMETER` 的 `max_string_size`(无权限则按 STANDARD 保守处理)。
  OB-Oracle 的上限不同:实测 `||` 拼出 4001、6000 字节都正常,超过 32767 字节报 `1489`;
  但 `UTL_RAW.CAST_TO_RAW` 对 32767 字符的串报 `6502`(RAW 变量超长)。OB-Oracle 的载荷上限按
  `CAST_TO_RAW` 的实际上限保守取值,具体数值在实现 PR 里用二分探测定下并写进单测。
- **`CLOB`/`NCLOB`/`LONG` 列**:归 `STRING` 但不能进 `STANDARD_HASH`,降级。(OB-Oracle 的
  `DBMS_CRYPTO.HASH` 直接接受 CLOB:`HASH(TO_CLOB('abc'),2)` 与 VARCHAR2 结果相同;GBK 库上
  `HASH(TO_CLOB('中文'),2)` 实测**自动按 AL32UTF8 取字节**,与 UTF-8 MD5 一致,不需要 `CONVERT`。
  因此 OB-Oracle 的 CLOB 列和超长载荷可以走 `TO_CLOB(载荷)` 路径而不必降级;但规范化载荷
  的拼接仍会在 32767 处被**静默截断**——实测 `RPAD('x',40000,'x')` 只得到 32767 字节、
  不报错。所以拼接本身必须在 CLOB 上做(`TO_CLOB(a) || ...`),不能先拼 VARCHAR2 再转。)
- **非 UTF-8 规范化字符集**:同 DM 现有规则。
- 11g 且 `DBMS_CRYPTO`、`DBMS_OBFUSCATION_TOOLKIT` 都不可执行:整表降级。
- `V$PARAMETER` 读不到(最小权限账号的常态,11g 实测 942):12c+ 按 `STANDARD` 即 4000 处理;
  11g 本无 `max_string_size`,按 4000,但 MD5 输入受 2000 字节约束(见 §6.1.1)。

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
| PR-0.5 | 修 bug | Oracle 连接设 `outputtypehandler`:NUMBER 取 `Decimal`、LOB 直接取值(§4.1.1 POC 结论);thick 开关 `DATAOPS_ORACLE_CLIENT_DIR`;`DPY-3010` 友好提示 | — | S |
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

| 编号 | 问题 | 影响 | 状态(2026-10-27) |
|---|---|---|---|
| V1 | 各版本的版本串/`ob_version()` 输出;能否据此区分 3.x/4.x 与模式 | 测试连接、存量类型提示 | OB-Oracle 已测:`ob_version()`=4.3.5.4;`VERSION()` 不存在;握手版本是代理的 5.6.25;模式看 `ob_compatibility_mode`。3.x 未测 |
| V2 | OB-Oracle 扩展类型码与文本格式 | §4.3.4 | OB-Oracle 已测,见 §4.3.4 |
| V3 | 三种方言的取消、超时、登录失败、连接断开的数值错误码 | 错误分类器 | OB-Oracle 已测,见 §5;Oracle、OB-MySQL 未测 |
| V4 | PyMySQL 在 OB-Oracle 下的转义行为、`NO_BACKSLASH_ESCAPES` 状态位 | §4.3.3 | OB-Oracle 已测:反斜杠是普通字符,PyMySQL 缺省转义会报 900 |
| V5 | `ob_query_timeout` 在两种模式下的设置方式与生效范围;`MAX_EXECUTION_TIME` 是否支持 | 超时 | OB-Oracle 已测:SET/ALTER SESSION/hint 都生效;`MAX_EXECUTION_TIME` 无效。OB-MySQL 未测 |
| V6 | OB 两种模式的系统库/系统 schema 名单;OB-Oracle `ALL_*` 视图的列差异 | 元数据 | OB-Oracle 已测,见 §4.3.2。OB-MySQL 未测 |
| V7 | OB-Oracle 是否支持 `FETCH FIRST n ROWS ONLY` | 分页 | 已测:`FETCH FIRST`/`OFFSET..FETCH`/`ROWNUM` 可用,`LIMIT` 不可用 |
| V8 | OBProxy 对流式结果集的空闲超时 | 大结果集、比对 | OB-Oracle 已测,见 §13.1 |
| V9 | OB 两种模式取 DDL 的方式 | DDL | OB-Oracle 已测:`DBMS_METADATA.GET_DDL` 与 `SHOW CREATE TABLE` 都可用 |
| V10 | OB-Oracle 的 `EXPLAIN PLAN FOR` / `DBMS_XPLAN` | 执行计划 | OB-Oracle 已测:只有 `EXPLAIN` / `EXPLAIN FORMAT=JSON` |
| V11 | OB-MySQL 上 MySQL 哈希表达式的输出是否与 MySQL 逐字节一致;OB-Oracle 上可用的 MD5 函数 | 哈希下推 | OB-Oracle 已测:只有 `DBMS_CRYPTO`,需转 AL32UTF8,约 1 万行/秒。OB-MySQL 未测 |
| V12 | 经 OBProxy 的 `KILL QUERY` 路由;OB-Oracle 的会话标识与 `KILL` 语法 | 会话控制台硬取消 | OB-Oracle 已测:SID=thread_id,经代理 `KILL QUERY`/`KILL` 可用 |
| V13 | 方案 A(PyMySQL 置 bit 27)能否连上 OB-Oracle;不能则试方案 B | 驱动选型 | **已测:方案 A 成立** |

### 13.1 V8:流式结果集的空闲断连(OB-Oracle 实测)

环境:OB 4.3.5.4 企业版 Oracle 租户,经 OBProxy;服务端 `net_write_timeout=60`、
`net_read_timeout=30`(秒)。用 `SSCursor` 读 `CONNECT BY` 生成的 200 字节宽行,读到第一行后
停顿,再继续读完。

| 行数 | 停顿 | `net_write_timeout` | 结果 |
|---|---|---|---|
| 60 000 | 75 秒 | 60(缺省) | 读完——剩余数据全部进了代理/网络缓冲,服务端没被写阻塞 |
| 200 000 | 75 秒 | 60(缺省) | 先从缓冲读出约 2 万行,随后 `2013 Lost connection` |
| 200 000 | 130 秒 | 60(缺省) | 同上,排空时 `2013` |
| 200 000 | 75 秒 | 会话设 300 | **读完** |

结论:**消费方停顿超过 `net_write_timeout`,且结果集大于沿途缓冲时,服务端会断开连接**;
断开不会立刻被客户端发现,要等缓冲读空才报 `2013`。这不是 OBProxy 自己的空闲超时,而是
服务端写超时。

处理:
- OB 两种模式的流式连接,在执行查询前 `SET net_write_timeout = <秒>`,取值不小于该连接的
  `cursor_max_hold_seconds`(会话控制台)或比对 / 导出 worker 的单段最大处理时间;
- 流式连接一律设 PyMySQL `read_timeout`,避免网络异常时永远挂住;
- `2013` 归入“连接断开”,比对按段重试(现有分段重试逻辑),不当作数据差异。

(顺带记录:本次 POC 经两级隧道访问,200 000 × 200 字节约 264 秒,约 160 KB/s,瓶颈在网络。
所以 §6.1.1 里“客户端哈希可能比库内 `DBMS_CRYPTO` 快”的判断,要在部署现场的网络上重测。)

---

## 14. 待你拍板的事项

1. **OB-Oracle 驱动**:POC 已证明方案 A 可行(§4.3.1),建议定为 A、不引入 pyoceanbase。
   代价是 `oceanbase_oracle_driver` 要继承 PyMySQL 私有类接管字段解码(§4.3.4),PyMySQL 需锁版本。
2. **11g**:已确认现场有 11.2.0.4,thick 开关必须做(PR-0.5)。待定的是 Instant Client 19c
   由谁放、放在哪:建议部署方自行下载放置,包内只带 README 步骤(许可与包体基线两方面都不宜内置)。
   另外 PR-0.5 里的 NUMBER 精度修复是现有 bug,建议不等其他拍板先做。
3. **新增 `rtrim_char` 比对规则**:改规范与三方实现,还是先只靠 `trim_strings`。
   (实测 OB-Oracle `CHAR` 返回带补齐空格,这条对跨族比对是实打实的问题。)
4. **Oracle 前端放开执行**(PR-6):在升 Certified 之前,是否先以 Preview 档放开。
5. **PR-0 / PR-1 先行**:实例到位前先做这两个纯重构,可以吗。
6. **测试实例**:OB-Oracle 已有一套(4.3.5.4,经 OBProxy,账号名下无表、可读其他业务
   schema)。还缺:Oracle 一套、OB-MySQL 租户一套;以及一个**可建表**的 schema,用来放类型
   全集的黄金表(本次 POC 只能用 `DUAL` 上的表达式,没测真实列的 UROWID/NCLOB 等)。
7. **OB-Oracle 哈希下推缺省关**(§6.1.1):`DBMS_CRYPTO` 约 1 万行/秒,建议缺省走客户端哈希,
   有大表实测后再定。同意吗。
8. **错误码 `600` 的内部码解析**(§5):是否接受分类器对 `600` 做锚定前缀解析这一个口子。

---

## 附录 A:参考资料

- OceanBase Python 连接 Oracle 模式(cx_Oracle + OBCI):
  <https://en.oceanbase.com/docs/common-oceanbase-database-10000000003453840>
- pyoceanbase:<https://pypi.org/project/pyoceanbase/>
- python-oracledb 文档:thin/thick 模式、`init_oracle_client`、`Connection.cancel()`
- Oracle `STANDARD_HASH`(12.1 起)、`DBMS_METADATA`、`DBMS_XPLAN` 官方文档
- 仓库内:`docs/design/compare-normalization-spec.md`、`docs/acceptance-dm-certified.md`、
  `docs/adr/0023-console-session-broker.md`、PR #154(Oracle Certified 前置项)
