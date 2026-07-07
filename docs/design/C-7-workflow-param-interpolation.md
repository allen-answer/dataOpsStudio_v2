# C-7 Workflow 节点参数化 + `${var}` 插值 设计稿

> 2026-07-07,Opus 4.8。方法:两路考古 —— 本仓 main 测绘 2.0 workflow 节点/spec、run 变量
> 现有机制(`when` 求值)、compare_run 执行接入点、child job 构造点;`ssh daily-server` 只读
> 1.x `~/dataops-studio/app/services/workflow_engine.py` 测绘 `${var}` 插值实现(语法/作用域/
> 类型/缺失行为/SQL 安全)。每条带 `文件:行号` 定位。设计稿不出现 IP/账号/密钥/绝对路径。
>
> **结论先行**:2.0 已有一半基建 —— `${var}` 占位符正则、内置确定性变量、触发时冻结变量快照,
> 全部在 `app/domain/workflow_when.py` 已落地,但只服务于节点 `when` 条件求值,**不碰节点 payload**。
> C-7 = 把同一套 `${var}` 插值扩展到**节点 payload 参数**,在 **run 推进器 enqueue 子 job 之前**
> (`worker.py:_build_workflow_child_job` 上游)对 `node.payload` 做一遍插值,再落成 child job payload。
> 1.x 的 `services/workflow_engine.py:_interpolate` 是成熟可移植参考(递归 dict/list/str + `${var}`
> + `${var | sql_in}` 过滤器 + `${nodes.<id>.<path>}` 节点输出引用)。**2.0 首版建议只移植标量
> `${var}` + `sql_in`/`csv` 过滤器,不移植 `${nodes.*}` 跨节点取值**(与 `when` 首版口径一致,
> 2.0 子 job 产物是 ResultRef 不是自由 dict)。SQL 注入面靠 `sql_in` 过滤器转义 + 首版限定插值
> 只进 compare_ref 的 `table_name`/`schema_name`/`sql`(标识符/字面量位),不裸拼可执行结构。

---

## 一、2.0 现状测绘(接入面)

### 1.1 节点 / spec 定义与 R7 白名单

`app/domain/workflow.py`:

- `WorkflowNode`(`workflow.py:81`):`id` / `job_kind` / `payload: dict[str,Any]`(`workflow.py:91`,
  **参数就装在这**) / `retry_policy` / `timeout_seconds` / `on_failure` / `when`。`model_config` frozen。
- `WorkflowSpec`(`workflow.py:146`):`nodes` + `edges` + 可选 `schedule`。**无 `default_variables` /
  `variables` 字段** —— 2.0 目前没有 workflow 级/run 级用户变量声明位(见 §3.3 变量来源缺口)。
- R7 两层校验(`workflow.py:104-117`):`job_kind` 必须在 `ALLOWED_WORKFLOW_NODE_KINDS`(R7 白名单,
  `forbidden_node_kind`),再须在 `SUPPORTED_WORKFLOW_NODE_KINDS_V1`(`workflow.py:30`:`sql_query` /
  `sql_explain` / `compare_run` / `lineage_analyze` / `export_excel`)。**参数化只改 payload 内容,
  绝不碰 `job_kind`;R7 校验点不动、不绕过**(见 §5.1)。

### 1.2 run 时变量现有机制(`when` 已铺好一半基建)

`app/domain/workflow_when.py` —— **C-7 直接复用的核心**:

- `_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")`(`workflow_when.py:37`):`${var}` 占位符正则,
  与 1.x 语法一致。
- `builtin_when_variables(now)`(`workflow_when.py:64`):内置确定性变量 `today`/`now`/`year`/`month`/
  `day`,**全 str**,与 1.x `_default_variables` 同集合。
- `_interpolate(expression, variables)`(`workflow_when.py:122`):`${var}` → 变量值。**当前实现把值
  `repr()` 成 Python 字面量交给受限 AST**(服务 `when` 表达式),C-7 的 payload 插值需要的是"值直接
  替换进字符串",语义不同,**不能直接复用这个函数,但正则 + 变量集合可复用**(见 §3.1)。
- 缺失变量行为(`workflow_when.py:129`):`unknown_when_variable` 抛 `WhenEvaluationError`(**严格,
  缺失即失败**,不留空)。`${nodes.*}` 引用显式拒绝(`workflow_when.py:125`:`unsupported_when_reference`)。

**变量冻结与来源**:

- 触发端点 `trigger_workflow_run`(`core.py:3328`)把 `"when_variables": builtin_when_variables(
  datetime.now(UTC))` 写进 run job payload(`core.py:3366`)—— **触发时刻冻结一份快照**。
- `when_variables_from_payload(payload)`(`workflow_when.py:75`):读回这份快照,形状校验(全 str),
  损坏/缺失回退 None。
- run 推进器 `_execute_workflow_run`(`worker.py:1185`)每步都读 `frozen_variables =
  when_variables_from_payload(job.payload)`(`worker.py:1203`),传给 `plan_workflow_step`
  的 `when_variables=`(`worker.py:1209`)。跨午夜不翻转、API 状态查询同源(`core.py:3527`)。

★ **这份"触发时冻结、执行器每步共用"的变量快照,正是 C-7 参数插值要用的同一个变量源** —— 无需
再造一套,只需扩展快照内容(§3.3)并在 enqueue 前用它插值 payload。

### 1.3 compare_run 节点执行 + 参数读取(插值目标形态)

- run 推进器 `plan_workflow_step`(`workflow_execution.py:104`)算出就绪节点 → `_build_workflow_child_job`
  (`worker.py:1966`)**把 `node.payload` 原样 `dict(node.payload)` 拷进 child job payload**
  (`worker.py:1967`)+ 补 `workflow_node_id`。★ **C-7 唯一注入点就在这行之前**:先插值 `node.payload`,
  再落 child job。
- child job 被 worker claim 后走 `_execute_compare_run`(`worker.py:721`):从 `job.payload` 读
  `source_id`/`target_id`(`worker.py:724`)、`source_ref`/`target_ref`(`worker.py:756`,经
  `_payload_compare_data_ref`,`worker.py:2523`)、`columns`/`rules`/`limits`。
- **compare_ref 可插值字段**(`schemas.py:137` `CompareDataRef`):
  - `kind: "table"|"sql"`(`schemas.py:138`)
  - `schema_name: str|None`、`table_name: str|None`(`schemas.py:139-140`)—— **标识符位**
  - `sql: str|None`(`schemas.py:141`)—— **kind=sql 时的原始查询,含 WHERE 等字面量位**
  - 无独立 `where` 字段:按日期/环境过滤要么进 `table_name`(如 `trades_${yyyymmdd}`),要么进 `sql`
    的 WHERE(如 `... WHERE trade_date = '${today}'`)。**这两类正是 C-7 的典型用例**。
- 建 SQL 时 `table_name`/`schema_name` 经 `_table_expression`(`dbclients/mysql_compare.py:38`)拼进
  `FROM`;`where` 经 `_where_clause`(`mysql_compare.py:148`)。**当前是字符串拼接**(见 §5.3 注入)。

### 1.4 现有字符串插值工具盘点

全仓 `${` 插值只有两处:`app/domain/workflow_when.py`(上述,服务 `when`);`db/migrations/
script.py.mako`(Alembic 模板 mako 语法,无关)。**没有通用 payload 插值工具** —— C-7 需新建一个
小模块(建议 `app/domain/workflow_interpolate.py`,纯函数零 IO,守 R1)。

---

## 二、1.x 参考(`services/workflow_engine.py`,移植依据)

1.x 有完整的节点 config 插值,语义成熟,**直接作为 C-7 的移植蓝本**:

### 2.1 语法

- `${var}` —— workflow / runtime 变量(`workflow_engine.py:_VARIABLE_PATTERN = re.compile(
  r"\$\{([^}]+)\}")`,与 2.0 `_PLACEHOLDER_RE` 同形)。
- `${var | filter}` —— 过滤器管道(`_interpolate_string`:按第一个 `|` 分割)。过滤器 `_apply_filter`:
  - `sql_in`(`_format_sql_in`):渲染成 SQL `IN(...)` 体。**数字裸出、字符串单引号 + `'`→`''` 转义、
    None→NULL、空 list→NULL**(避免 `IN ()` 语法错)。★ **这是 SQL 注入防护的关键过滤器**。
  - `csv`(`_format_csv`):逗号拼接,无引号(内部数字 ID 用)。
- `${nodes.<id>.<dot.path>}` —— 已完成节点输出引用(XCom/GHA `outputs.<step>` 风格,
  `_resolve_placeholder_value`:走 `outputs` dict,支持 dict key + list index)。
- **无 `${var:default}` 默认值语法** —— 1.x 缺失变量直接 `KeyError`。

### 2.2 作用域 / 类型 / 缺失行为

- 变量作用域(`run_workflow`,`workflow_engine.py`):`resolved_vars = {**_default_variables(),
  **workflow.default_variables, **(variables or {})}` —— 三层合并:内置 < workflow 默认 < 运行时传入。
- `params` 节点(`_merge_params_output`):节点输出回写 variable scope,下游可直接 `${biz_date}`。
  **标量转 str、list/dict 保留原类型**(让 `${ids | sql_in}` 拿到真 list)。
- 求值时机(`run_workflow` 主循环):**先 `when` 判定,再 `_interpolate(node.config)`**,插值 KeyError →
  节点 `FAILED`,`error="unresolved variable: <name>"`,阻塞下游。
- `_interpolate`(递归):str→占位符替换,dict/list→递归,其他类型透传。

### 2.3 与 2.0 的差异(哪些不移植)

| 1.x 能力 | 2.0 首版 | 理由 |
|---|---|---|
| `${var}` 标量 | ✅ 移植 | 核心诉求 |
| `${var \| sql_in}` / `csv` | ✅ 移植 | SQL 注入防护刚需 |
| `${nodes.<id>.<path>}` | ❌ 不移植 | 2.0 子 job 产物是 ResultRef 非自由 dict;与 `when` 首版口径一致(`workflow_when.py:8-10` 已明写不开放) |
| `params` 节点回写 scope | ❌ 不移植 | `params` 不在 `SUPPORTED_WORKFLOW_NODE_KINDS_V1`;无 params 就无回写需求 |
| `workflow.default_variables` | ⚠️ 需新增字段 | 2.0 `WorkflowSpec` 无此字段(§3.3) |
| 运行时传入 `variables` | ⚠️ 需扩展触发端点 | 见 §3.3 |
| 缺失变量 → 失败 | ✅ 沿用严格语义 | 与 2.0 `when` 一致,不臆造留空 |

---

## 三、2.0 落地设计

### 3.1 插值语法(首版)

- `${var}` —— 变量替换(标量,渲染为字符串直接嵌入)。
- `${var | sql_in}` / `${var | csv}` —— 过滤器(list/多值 → SQL 安全片段)。
- **不支持** `${nodes.*}`(拒绝,报 `unsupported_param_reference`)、`${var:default}`(首版不做,
  拿不准是否需要,标注待定 —— 若有需求二期加)。
- 缺失变量:**严格失败**(节点 `FAILED`,`error="unresolved_param_variable: <name>"`),与 `when`
  的 `unknown_when_variable` 口径一致。不留空、不臆造默认。

### 3.2 求值层(关键决策:在 enqueue 子 job 前,不在子 job 执行期)

**决策:插值在 run 推进器 enqueue 子 job 之前做**(`worker.py:_build_workflow_child_job` 上游),
**不在 compare_run 执行期做**。理由:

1. **变量源就位**:`_execute_workflow_run` 已有 `frozen_variables`(`worker.py:1203`),直接可用;
   compare_run 子 job 执行期拿不到 run 级变量(payload 只有节点自己的参数)。
2. **单一职责**:child job payload 一旦落库就是"已解析的最终参数",子 job 执行器无需知道 `${var}`
   存在,`_execute_compare_run` 零改动。
3. **可观测**:插值后的 payload 进 child job 落库,run 状态查询/审计能看到实际跑的参数(注意 R5 脱敏,
   §5.2)。
4. **幂等**:`plan_workflow_step` 幂等,但 enqueue 有 `uq_jobs_workflow_node_per_run` 去重
   (`worker.py:1218`),同节点只 enqueue 一次,插值也只做一次,变量快照冻结 → 结果确定。

**改法**(外科手术,diff 小):

- 新建 `app/domain/workflow_interpolate.py`:`interpolate_payload(payload: Mapping, variables:
  Mapping[str,str]) -> dict`,递归 dict/list/str,`${var}` 替换 + `| sql_in`/`csv` 过滤器,
  缺失/`nodes.*` 抛 `ParamInterpolationError`(纯函数,零 IO,守 R1)。可复用 `_PLACEHOLDER_RE`。
- `_build_workflow_child_job(run_job, node)` 加一个 `variables` 参数,内部先
  `payload = interpolate_payload(node.payload, variables)` 再构造 Job(`worker.py:1967` 那行改成
  插值结果)。调用点 `worker.py:1215` 传入 `frozen_variables`(与 `plan_workflow_step` 同源)。
- 插值失败:enqueue 前抛 → 该节点视为 `FAILED`(需在推进器捕获,落节点错误态,走既有
  `on_failure` 语义;与 `when` 求值异常 `workflow_execution.py:151-159` 的 FAILED 处理对齐)。

### 3.3 变量来源(需补齐的缺口)

现状只有 `builtin_when_variables`(5 个内置)。C-7 要"按日期/环境跑不同对比",需要**用户可传变量**。
两个来源,建议分 PR:

1. **workflow 级默认变量**:`WorkflowSpec` 加 `variables: dict[str,str] = {}` 字段(对齐 1.x
   `default_variables`)。校验:key 命名限 `[A-Za-z_][A-Za-z0-9_]*`、value 限 str、数量/长度上限、
   **禁与内置变量名冲突**(或定义覆盖优先级)。存进 `dag_jsonb`,触发时并入快照。
2. **触发时运行时变量**:`trigger_workflow_run`(`core.py:3328`)接受可选 request body
   `{"variables": {...}}`,并入 `when_variables` 快照(`core.py:3366` 那处扩展)。API schema 加校验。
   合并优先级(对齐 1.x):`builtin < spec.variables < 触发时传入`。

★ 合并后的完整快照仍写进 run payload 的 `when_variables`(**同一份**服务 `when` + payload 插值),
`when_variables_from_payload` 的形状校验(全 str)需保持;若引入 list 型变量(为 `sql_in`)需放宽
形状校验或另开 key(拿不准,标注:首版可只支持 str 变量,`sql_in` 二期)。

### 3.4 缺失 / 类型 / 转义处理

- **缺失**:严格失败(§3.1)。
- **类型**:首版变量全 str(与 `when` 快照一致)。`${var}` 直接字符串替换。`sql_in`/`csv` 若首版
  只支持 str,则单值处理;list 型变量待 §3.3 快照放宽后二期支持。
- **转义 / 注入**:见 §5.3 —— 标识符位(`table_name`/`schema_name`)与字面量位(`sql` 的 WHERE)
  处理策略不同,是 C-7 安全核心。

### 3.5 分 PR 建议

- **PR1**(插值引擎 + 接入):新建 `workflow_interpolate.py`(`${var}` + `sql_in`/`csv` + 严格缺失),
  接进 `_build_workflow_child_job`,变量源先只用现有 `builtin_when_variables`(**先打通链路,内置变量
  即可参数化 `table_name: "t_${today}"`**)。单测覆盖插值 + 缺失 + 过滤器 + 注入用例。
- **PR2**(变量来源):`WorkflowSpec.variables` 字段 + 触发端点 `variables` body + 合并优先级 + 校验。
- **PR3**(可选,二期):list 型变量 / `sql_in` 完整支持(放宽快照形状);评估 `${var:default}` 需求。

---

## 四、范围建议(首版)

- **节点**:首版只对 **compare_run** 做参数化插值(任务聚焦 C-7 compare 节点)。实现上
  `interpolate_payload` 对任意节点 payload 通用,但**首版只在 compare_run 的 payload 上启用**
  (或对所有 `SUPPORTED_WORKFLOW_NODE_KINDS_V1` 统一启用,取决于是否要 sql_query 节点也参数化 ——
  建议**统一启用**,因为插值是纯 payload 变换,对不含 `${}` 的 payload 无副作用/幂等)。
- **语法**:`${var}` + `| sql_in` + `| csv`。不做 `${nodes.*}`、不做 `${var:default}`。
- **变量**:PR1 只内置 5 变量;PR2 加 spec/触发变量。
- **插值目标**:compare_ref 的 `table_name` / `schema_name` / `sql`(+ 理论上 payload 任意 str 值,
  但典型用例是这三个 + `source_id`/`target_id` 若要按环境切数据源)。
- **不做**:跨节点取值、循环/矩阵展开(同一 workflow 一次跑一组参数;多组靠多次触发)、条件默认值。

---

## 五、红线注意(详述)

### 5.1 R7(节点白名单)——参数化不得绕过 kind 校验

- 插值**只改 `node.payload` 内容,绝不改 `node.job_kind`**。`_build_workflow_child_job` 里
  `JobKind(node.job_kind)`(`worker.py:1971`)在插值之后仍用**原始 `job_kind`**,不接受 `${var}`
  插到 `job_kind`(payload 插值不碰这个字段,天然隔离)。
- **禁止**变量值里塞节点 kind 或让插值结果改变节点类型。`interpolate_payload` 只处理 payload dict,
  spec 结构(nodes/edges/job_kind)在插值前已过 `WorkflowSpec.model_validate` R7 双门禁
  (创建 `core.py:3226` + enqueue `core.py:3345`),插值发生在 R7 校验**之后**、纯数据层面,不回炉
  spec 校验、也无法引入新节点。
- 单测:构造 `job_kind` 含 `${}` 的畸形节点 → spec 校验就该拒(`forbidden_node_kind`),不进插值。

### 5.2 R5(日志脱敏)——插值后参数含敏感值不进日志

- 插值后的 payload 落 child job(`worker.py:1214-1233` enqueue 日志已在打 `node_id`/`kind`,
  **未打 payload**,保持)。**新增插值逻辑禁止 `logger.*` 打 rendered payload / 变量值**。
- 若变量值可能是敏感值(如环境切换带的连接标识),插值失败错误信息只报**变量名**不报**变量值**
  (`unresolved_param_variable: <name>`,不含 value),与 `when` 的 `unknown_when_variable`(只报 name)
  一致。强制走 structlog 脱敏 processor(R5),禁 stdlib logging。
- child job payload 落库本身是既有行为(compare_run payload 一直落库),C-7 不新增敏感面;但审计/
  状态查询回显 payload 时若含插值后敏感值,沿用既有脱敏。

### 5.3 注入安全(SQL)——`${var}` 进 SQL/标识符位必须转义或参数化

**这是 C-7 最需谨慎处**。compare_ref 三个插值目标风险不同:

- **`table_name` / `schema_name`(标识符位)**:进 `_table_expression`(`mysql_compare.py:38`)拼 `FROM`。
  **不能用参数占位符**(标识符不能参数化)。防护:
  - 插值后对结果做**标识符白名单校验**(限 `[A-Za-z0-9_$]`,或按方言 quote),拒绝含空格/引号/分号/
    注释符的值 → 抛 `unsafe_identifier`。
  - 或限制变量值来源为受信内置/spec 声明(非任意外部输入),但仍应加白名单兜底(纵深防御)。
- **`sql`(kind=sql,原始查询)**:用户本就提供整条 SQL,`${var}` 插进 WHERE 字面量位。防护:
  - **字面量位强制走 `| sql_in` 或引号转义**(`_format_sql_in` 已做 `'`→`''`),**禁止裸
    `WHERE x = ${var}`** 直接拼(值可注入 `1 OR 1=1`)。文档/校验引导用户写 `WHERE x IN (${ids|sql_in})`
    或 `WHERE d = '${today}'`(内置 date 值受控)。
  - 这里无法真正参数化(compare 走的是自定义 hash 查询构造,不是 execute(sql, params)),所以
    **转义是唯一防线** —— 首版建议**保守**:`sql` ref 的插值只允许受信内置变量(date 类,格式受控),
    用户自由变量进 `sql` 需过 `sql_in`/严格校验(拿不准可否完全防住任意 SQL 注入,标注:若不能,
    首版 `sql` ref 不开放自由变量插值,只 `table_name` 参数化)。
- **通用**:`interpolate_payload` 本身只做字符串替换,**安全责任在过滤器 + 落点校验**。单测必须含
  注入用例(`'; DROP`、`1 OR 1=1`、`--` 注释、反引号/引号逃逸),断言被转义或被拒。

---

## 六、测试要求

- **插值引擎单测**(`workflow_interpolate.py`,纯函数好测):
  - `${var}` 替换、多占位符、嵌套 dict/list 递归、无占位符透传。
  - `| sql_in`:数字裸出、字符串单引号 + `''` 转义、None→NULL、空 list→NULL、单值。
  - `| csv`:逗号拼接无引号。
  - 缺失变量 → `ParamInterpolationError`(错误只含变量名,不含值,守 R5)。
  - `${nodes.*}` → 拒绝(`unsupported_param_reference`)。
  - **注入用例**:`'; DROP TABLE`、`1 OR 1=1`、`--`、引号逃逸,断言转义/拒绝。
  - 标识符位白名单:含空格/分号/引号的 `table_name` → `unsafe_identifier`。
- **接入单测**(worker):`_build_workflow_child_job` 用含 `${today}` 的 compare payload + 冻结快照 →
  child job payload 是解析后的实际值;插值失败 → 节点 FAILED 走 `on_failure`。
- **R7 单测**:`job_kind` 不受 payload 插值影响;畸形 spec 在插值前被 R7 拒。
- **端到端**(接真后端,遵 CLAUDE.md §4):触发一个 compare_run workflow,payload `table_name`/`sql`
  含 `${today}`,断言子 job 真按当日日期跑对比、run 成功、结果 rows 正确。贴真走通证据(响应体/日志)。
- **确定性**:同一 run 跨多步推进插值结果一致(变量快照冻结,不随推进时刻漂移),与 `when`
  的跨午夜不翻转测试(`test_workflow_when.py`)同范式。

---

## 附:关键接入点速查

| 关注点 | 位置 |
|---|---|
| 节点 payload(参数装载处) | `app/domain/workflow.py:91` |
| R7 白名单 / 支持集 | `app/domain/workflow.py:26-38, 104-117` |
| `${var}` 正则 + 内置变量 | `app/domain/workflow_when.py:37, 64` |
| 变量快照读回 | `app/domain/workflow_when.py:75` |
| 触发时冻结快照 | `app/api/routes/core.py:3366` |
| run 推进器读快照 | `app/worker.py:1203-1211` |
| **C-7 注入点:child job 构造** | `app/worker.py:1966-1984`(`_build_workflow_child_job`) |
| enqueue 调用点(传变量) | `app/worker.py:1214-1215` |
| compare_run 执行读 payload | `app/worker.py:721-757` |
| compare_ref 可插值字段 | `app/api/schemas.py:137-149`(`CompareDataRef`) |
| SQL 拼接落点(注入面) | `app/dbclients/mysql_compare.py:38, 148` |
| 1.x 插值蓝本 | `services/workflow_engine.py`(`_interpolate` / `_apply_filter` / `_format_sql_in`) |
