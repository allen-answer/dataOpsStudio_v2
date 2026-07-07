# L-3 存储过程深度解析 设计稿

> 2026-07-07,Opus 4.8。方法:两路考古 —— 服务器 1.x 真源码
> (`ssh daily-server`,`~/dataops-studio/app/lineage/`,**只读**)逐能力测绘
> `segments.py`(1099 行)+ `variables.py`(208 行)+ `analyzer.py` 消费面;
> 本仓 main 测绘 2.0 血缘域现状(`app/domain/lineage/plsql.py` / `parser.py` /
> `models.py`)。每条能力带 `文件:行号`,基于源码事实,拿不准标「未确认」。
> 用途:L-3「存储过程深度解析」实现底稿。
>
> **结论先行**:2.0 现有 `plsql.py:8 split_plsql_statements`(裸分号切分 + DML 前缀
> 过滤 + **剥掉全部注释**)相对 1.x 丢失了 8 类能力:控制流感知切段、业务标题、
> 游标补链、动态 SQL 告警、PL/SQL 变量、局部变量误判过滤、PACKAGE/嵌套/匿名块/
> TRIGGER、step-level 报告字段。L-3 = 把 1.x `segments.py` + `variables.py` 这套
> 移植进 2.0,接在 `parser.py:108 _parse_or_split_plsql` 这个缝上。**纯正则 +
> 字符串扫描 + sqlglot AST,零 DB 依赖 —— R1 天然满足**。

---

## 一、1.x segments/variables 能力测绘(移植依据)

以下每条 = 一个可独立移植的能力单元,标注 1.x 源码位置与判据。

### A1. 过程体切段主入口 —— `segments.py:502 extract_procedure_segments`

把 `CREATE PROCEDURE/FUNCTION/PACKAGE BODY/TRIGGER` 块内嵌套的 DML 逐条抽出,
返回每条记录:`procedure_name / procedure_kind / segment_index / sql /
confidence / line_start / line_end / preceding_comment / parse_status /
cursor_sources / trigger_source`。三条扫描路径合并:

1. **顶层匿名块**(`segments.py:524`,PR16):`_RE_ANON_PLSQL_BLOCK`(`:128`)
   匹配 `DECLARE … BEGIN` / 裸 `BEGIN`,不落在任何 CREATE 范围内的算
   `procedure_kind=ANONYMOUS`。否则整脚本会因 `DECLARE` 行让 sqlglot 解析失败。
2. **CREATE 过程/函数/触发器**(`:565`):`_RE_PROC_HEADER`(`:115`)匹配
   `CREATE [OR REPLACE] [DEFINER=x] (PROCEDURE|FUNCTION|PACKAGE BODY|TRIGGER) name`。
3. **PACKAGE BODY 内嵌套 proc**(`:568`,PR13):`_find_nested_proc_scopes`
   (`:670`)用 `_RE_NESTED_PROC_HEADER`(`:135`)在包体范围内再扫
   `PROCEDURE|FUNCTION name`(无 CREATE 前缀),每个嵌套 proc 独立成 scope,
   包级声明区(cursor 声明)合并进每个嵌套 proc 的 declaration_region(`:602`)。

块边界靠 **token 平衡扫描**(`_find_block_end` `:641`):按 `BEGIN`/`END` 配对,
但 `_is_block_end`(`:151`)+ `_CONTROL_END_KEYWORDS`(`:146` = IF/LOOP/CASE/
RECORD/FOR/WHILE/XMLELEMENT)把 `END IF;` / `END LOOP;` / `END CASE;` / 裸
`case…end` 从块级 END 计数里排除 —— 否则 depth 错位会漏切后续 INSERT。

### A2. 单块内逐语句切分 —— `segments.py:718 _iter_procedure_body_segments`

在一个 body 内按顶层 `;` 切段(`:733` 字符扫描),同时:
- **跳字符串字面量**(`:736`)避免字符串内 `;` 误切;
- **跟踪嵌套 BEGIN/END depth**(`:752`)避免嵌套块内 `;` 结束外层段;
- 每段找首个 DML 关键字(`_RE_BODY_DML` `:174` = WITH/SELECT/INSERT/
  REPLACE INTO/UPDATE/DELETE/MERGE/CREATE … TABLE/TRUNCATE),从 DML 处起分析,
  **剥掉前面的 IF…THEN / CASE 等控制流壳子**(`:795`)。
- 返回 `_BodySegment`(`:8`)带 body 内 offset(`dml_start` / `end`),上层
  据此算 `line_start` / `line_end`(`_line_of` `:700`)。
- dedupe key = 规范化(空白压平)文本(`:612`),但**喂 sqlglot 用保留换行的
  原文**(`clean_procedure_segment` `:708`)—— 行注释 `--` 必须靠换行终止,
  压平会把列名吞进注释。

### A3. 业务标题注释切段 —— `segments.py:499 _RE_PURE_COMMENT_PREFIX` + `:849 _strip_comment_prefix`

判据:DML 关键字**前面的前缀**若 `fullmatch` 纯空白 + 行注释 `--…` + 块注释
`/* */`(即没有 IF/CASE 控制流壳子),则认为这段前缀就是**业务标题**,原样保留
(`:802`),抽出标题文本挂到 `preceding_comment`。`_strip_comment_prefix`(`:849`)
剥 `--` / `/* */` 标记、多行用空格连、截断 200 字符。

标题最终去向(`analyzer.py:608` → `report.py:329`):`preceding_comment` 直接进
`process_steps[].preceding_comment` 给前端 step 卡片显示。另有一条独立的
`aggregation.py:38 extract_statement_title` —— 从 sqlglot 节点 `.comments`
挑第一段非空文本作标题,且用 `_looks_like_oracle_hint`(Oracle hint 词表)过滤
`/*+ parallel(...) */` 之类 hint,避免误当业务标题。

> **切段模式**:不是靠「`-- 步骤N:` 固定前缀」,而是**任意纯注释前缀**都当标题。
> 更通用(中文业务标题 `-- 集中交易日终` 直接生效),无需约定注释格式。

### A4. 游标补链 —— cursor FOR loop source tracking

三个协作部件:
1. **内联游标**(`segments.py:451 _strip_cursor_for_prefix_with_sources`):段以
   `FOR rec IN (SELECT … FROM t) LOOP <DML>` 开头时,剥掉 `FOR…LOOP` 壳子,并从
   括号内 SELECT 抽源表挂到 `_BodySegment.cursor_sources`。
2. **显式游标声明**(`_RE_CURSOR_DECL` `:196` + `_collect_cursor_decls` `:202`,
   PR7/PR8):扫 `CURSOR name [(参数)] IS|AS SELECT …;`,建 `{游标名: [源表]}` 映射;
   `FOR rec IN cur_name LOOP` 引用形式靠此回填(`:315`),支持带调用参数
   `cur_name(a,b)`(`:326`)与数值范围 `FOR i IN 1..N`(非游标,不阻断)。
3. **LOOP 范围继承**(`_collect_loop_scopes` `:250`,PR2):扫全 body 找每个
   `FOR…LOOP … END LOOP` 范围(嵌套独立、取最内层),给被 `;` 切碎、落在 LOOP 体内
   但丢了上下文的**多个** DML 段(`:832`)统一补同一份 `cursor_sources`。

源表提取用轻量 `_extract_cursor_select_tables`(`:422`,只取顶层 FROM/JOIN 标识符,
不走 sqlglot,近似即可)。补链的最终消费在 `analyzer.py:263 _cursor_supplemental_edges`:
`INSERT INTO tgt VALUES (rec.col)` 的 VALUES 不是 SELECT,`_graph_edges` 看不到
`src → tgt` 边,这里据 `cursor_sources` × DML target 补
`edge_type=CURSOR_LOOP_INSERT / confidence=medium` 的边(去重比对现有边)。

### A5. 动态 SQL —— `segments.py:873 extract_dynamic_sql_segments`

尽力解析 + 告警,三条精确路径(**不做字面量兜底**,`:928` 注释:曾有 20+ 字符
字面量兜底,在大过程里产生海量误报,已删):
- **EXECUTE IMMEDIATE / sp_executesql 带字面量**(`:884`):直接抽引号内 SQL,
  confidence=high。
- **MySQL PREPARE stmt FROM @var / 字面量**(`:893`):字面量 high;`@var` 回查
  `SET @x := '…'` 赋值(`_capture_session_var_assignments` `:942`)。
- **Oracle `EXECUTE IMMEDIATE v_sql`,`v_sql` 由拼接赋值**(`:906`):回查
  `_capture_plsql_var_assignments`,`_rebuild_concat`(`:1019`)把 `'INSERT INTO '
  || p_t || ' SELECT …'` 重建为 `INSERT INTO :p_t SELECT …`(变量段替 `:placeholder`
  让 sqlglot 能解析),confidence=low。
- **无法解析的变量**(参数/包变量/外部传入,`:917`):出占位段
  `-- unresolved EXECUTE IMMEDIATE variable: x`,confidence=`unresolved`,触发
  `dynamic_sql` warning。`_looks_like_lineage_sql`(`:1091`)过滤非 DML 拼接。

### A6. PL/SQL 局部变量 / `${var}` —— `variables.py`

- **`script_variables`**(`variables.py:8`):抽三种占位符
  (`variable_names` `:31`)`${name}` / `:name` / `@name`,每个记 `name /
  placeholder / assigned_value`。`assigned_value`(`:43`)找 `name := …` 或
  `name = …` 的右值。**必须在 normalize 之前抽**(`analyzer.py:64` 注释:normalize
  会把 `${name}` → `:name`,之后识别不出原始模板名)。
- **`package_variables`**(`:90`,PR3):抽 `PACKAGE BODY … IS/AS` 顶层与
  `DECLARE` 块的 `name [CONSTANT] TYPE := value;` 声明,带 `kind`
  (package_constant / package_variable / declare_constant / declare_variable),
  `_DECL_NOISE`(`:121`)黑名单排除 CURSOR/TYPE/PROCEDURE 等关键字误识别。
- **`all_plsql_local_names`**(`:143`,PR17):扫所有声明区(PACKAGE 顶层 /
  PROCEDURE·FUNCTION 的 IS/AS→BEGIN 段 / DECLARE 块)收集**所有局部变量名**,
  **不进 result.variables**(避免污染前端面板),专用于**过滤误判**:
  `SELECT INTO v_row FROM ods.orders` 被 sqlglot 重写成 `CREATE TABLE v_row AS
  SELECT…`,`v_row` 会混进 tables 列表;`analyzer.py:196` 据此名集把它 drop;
  同时防 `v_row.id` 记录字段引用被误归到表 `v_row`。

### A7. MERGE / BULK COLLECT / TRIGGER / 嵌套块等特殊结构

- **MERGE**:切段层无特判(`_RE_BODY_DML` 含 MERGE),列级由下游 sqlglot 处理。
  但 `_RE_CURSOR_DML_TARGET`(`analyzer.py:288`)+ `_DML_TARGET_KEYWORD_BLACKLIST`
  (`:298`)防 MERGE 的 `WHEN MATCHED THEN UPDATE SET` 里 `SET` 被误当 target。
- **BULK COLLECT + FORALL**(`analyzer.py:475 _bulk_collect_supplemental_edges`):
  `SELECT … BULK COLLECT INTO v FROM tabA;` + `FORALL … INSERT INTO tabB VALUES
  (v(i).c)` → 建 `var → 源表` 映射,匹配引用同名数组的 DML 段,补
  `edge_type=BULK_COLLECT / medium` 边。
- **UDF 补链**(`analyzer.py:320 _udf_supplemental_edges`,PR4):FUNCTION 段体内
  `SELECT … FROM ods.txn`,而 `INSERT … VALUES (pkg.fn())` 自身无 source →
  补 `edge_type=UDF_CALL / medium` 边。
- **TRIGGER 补链**(`analyzer.py:413 _trigger_supplemental_edges`,PR15):从头部
  `AFTER … ON tabA`(`_RE_TRIGGER_SOURCE` `:121`)抽触发源表,连体内 DML target,
  补 `edge_type=TRIGGER / medium` 边。
- **REPLACE INTO**(`segments.py:58 extract_replace_segments` + `:82
  _parse_compatible_segment`):MySQL `REPLACE INTO … SELECT` 改写成
  `INSERT INTO` 让 sqlglot 认,打 `_lineage_dml_type=REPLACE` 标记。

### A8. 输出形态 —— 段如何喂血缘 + 报告新增字段

`analyzer.py:80-103` 编排:`dynamic_sqls` + `procedure_sqls` 与主体一并
`parse_segments`(容错)→ 展平进 `deduped_statements` 走同一 `_analyze_statement`
(所以段落照样拿到**表级/列级**血缘)。段元数据另行保留:
- 每段 `parse_status`(`:86`):单独喂 sqlglot 判 `parsed` / `unsupported`,
  前端可高亮「这一段无法静态解析」。
- `base_result` 多出字段(`:204`):`variables` / `dynamic_sql_count` /
  `dynamic_sql_segments` / `procedure_segments`,以及 4 类 supplemental
  `graph_edges`(带 `edge_type` / `confidence` / `reason`)。
- `report.py:308 _process_steps_from_segments`:`procedure_segments` →
  `process_steps`(`procedure_name / kind / segment_index / dml_keyword /
  line_start / line_end / preceding_comment / parse_status`)给前端 step 视图。
- `_collect_procedure_operations`(`analyzer.py:179`):段按 `line_start` 顺序产
  ops,把 procedure 内 `TRUNCATE→INSERT` 顺序拿回来(顶层 statements 因 dedup
  会打乱)—— 这条给 L-4 refresh_mode 判定喂序。

---

## 二、2.0 现状与差距

### 2.0 现状(本仓 main,只读)

- **`plsql.py:8 split_plsql_statements`**:`_procedure_body`(`:14`)用正则找首个
  `BEGIN` 到末个 `END` 截出 body → `_split_semicolon_statements`(`:29`,跳字符串
  的分号切分)→ 只留 `INSERT|UPDATE|MERGE|DELETE|SELECT` 开头的段。
  `_strip_line_comments`(`:25`)**把每行 `--` 之后全删** —— 业务标题当场丢失。
- **`parser.py:108 _parse_or_split_plsql`**:先 `sqlglot.parse` 整脚本,若成功
  且 `_looks_like_plsql`(`:1302`,仅判 `CREATE PROCEDURE`/`CREATE OR REPLACE
  PROCEDURE` 字串)为假 → 用顶层 statements;否则退回 `split_plsql_statements`。
- **`models.py:125 LineageReport`**:envelope `extra="allow"`,**已预留**字段
  `variables`(`:144`)/ `dynamic_sql_count`(`:146`)/ `dynamic_sql_segments`
  (`:147`)/ `procedure_segments`(`:148`)、`TableLineageEdge`(`:24`,含
  `inferred` 但**无** `edge_type`/`confidence`/`reason`)。
- 每 statement 走 `parser.py:121 _analyze_statement` → qualify + sqlglot lineage
  拿列级,或 lenient 降级表级(`:932`)。L-4 `semantic.py` 已在
  `docs/design/L-4-semantic-lineage.md` 落地(refresh/roles/observations)。

### 差距矩阵(1.x 有 → 2.0 缺)

| # | 能力 | 1.x 位置 | 2.0 现状 | 影响 |
|---|---|---|---|---|
| G1 | 控制流感知切段(token 平衡、跳 END IF/LOOP) | segments.py:718 | 裸分号 + BEGIN/END 截取 | 嵌套块/控制流壳子混入或漏切 DML |
| G2 | 业务标题注释 | segments.py:849 | `:25` 直接删注释 | step 卡片无标题 |
| G3 | 游标补链(内联/显式/LOOP 继承) | segments.py:250/451 | 无 | `INSERT VALUES(rec.col)` 断链 |
| G4 | 动态 SQL 抽取 + 告警 | segments.py:873 | 无 | 动态 SQL 静默丢失,无 warning |
| G5 | 变量抽取(`${}`/`:`/`@` + 包/DECLARE) | variables.py:8/90 | 无(字段空) | 前端变量面板空 |
| G6 | 局部变量误判过滤 | variables.py:143 | 无 | `v_row` 等混进 tables |
| G7 | PACKAGE BODY / 嵌套 proc / 匿名块 / TRIGGER | segments.py:502 | 仅 `CREATE PROCEDURE` 粗判 | 这些形态整体不分析 |
| G8 | step-level 报告字段(line/parse_status/proc_name) | analyzer.py:204 / report.py:308 | 字段预留但未填 | 无 step 视图 |
| G9 | 补链边元数据(edge_type/confidence/reason) | analyzer.py 4 函数 | `TableLineageEdge` 无这些字段 | UDF/BULK/TRIGGER 补链无处落 |

### 2.0 接入点

唯一接缝 = **`parser.py:108 _parse_or_split_plsql`**。当前它返回 `list[str]`;
L-3 让它(或新增编排)额外产出 `procedure_segments`(带 title/line/cursor_sources/
proc_name/kind/parse_status),既把段 SQL 喂进现有 `_analyze_statement` 循环拿列级,
又把段元数据留给报告与补链。`_looks_like_plsql`(`:1302`)需扩到覆盖 FUNCTION /
PACKAGE BODY / TRIGGER / DECLARE 匿名块。

---

## 三、2.0 落地设计(分 3 PR)

**总原则**(遵 CLAUDE.md §1):移植 1.x 已验证逻辑,不重新发明;纯函数、无 DB;
diff 按 PR 切小。1.x 的段 SQL 复用 2.0 已有的 qualify+lineage 列级管线,**不搬
1.x 的 dml/columns 子模块**(2.0 用 sqlglot 原生 lineage,精度已 ≥ 1.x)。

### PR1 —— 新增 `segments` 模块 + 切段/标题/变量/动态 SQL(骨架,无补链)

新文件 `app/domain/lineage/segments.py`,从 1.x `segments.py` 移植:
`extract_procedure_segments` + `_iter_procedure_body_segments` + `_find_block_end`
+ `_is_block_end` + `_strip_comment_prefix` + `extract_dynamic_sql_segments` +
全部正则常量。新文件 `app/domain/lineage/variables.py`(或并入现 `plsql.py`):
移植 `script_variables` / `package_variables` / `all_plsql_local_names`。

`parser.py` 接线(外科手术,`analyze_sql_lineage` 内):
1. `_parse_or_split_plsql` 之外新增 `procedure_segments = extract_procedure_segments(sql)`
   与 `dynamic_sql_segments = extract_dynamic_sql_segments(sql)`;段 SQL 并入待分析
   statements(容错解析,失败不阻断)。
2. 每段标 `parse_status`(单独试解析);业务标题填进 `report.statements[].title`
   / step 字段。
3. 填充已预留的 envelope 字段:`report.variables` / `dynamic_sql_count` /
   `dynamic_sql_segments` / `procedure_segments`。
4. `all_plsql_local_names` 过滤 `report.tables` 里的局部变量假表(G6)。
5. 每条无法解析的动态 SQL 段 → `LineageWarning(code="dynamic_sql", …)`。
6. 扩 `_looks_like_plsql` 覆盖 FUNCTION / PACKAGE BODY / TRIGGER / 匿名 DECLARE 块。

**关键实现注意**(1.x 踩过的坑,移植时保留):段落喂 sqlglot **必须保留换行**
(`clean_procedure_segment`),不可用压平文本;`script_variables` 在
`_normalize_dialect` 相关 normalize **之前**抽。

### PR2 —— 游标 / UDF / BULK COLLECT / TRIGGER 补链边

1. 扩 `models.py TableLineageEdge`:加 `edge_type: str | None`、`confidence:
   str | None`、`reason: str | None`(默认 None,向后兼容,envelope 不破)(G9)。
2. 移植 `analyzer.py` 4 个 supplemental 函数 → 2.0(建议放
   `app/domain/lineage/segments.py` 或独立 `supplemental_edges.py`):
   `_cursor_supplemental_edges` / `_udf_supplemental_edges` /
   `_bulk_collect_supplemental_edges` / `_trigger_supplemental_edges`,
   连同 `_RE_CURSOR_DML_TARGET` + `_DML_TARGET_KEYWORD_BLACKLIST` 一并搬。
3. `parser.py analyze_sql_lineage` 在 statement 循环后调这 4 个函数,把返回边
   `_append_unique` 进 `report.graph_edges`(去重比对 source/target)。
4. 游标源表提取复用移植过来的 `_extract_cursor_select_tables`。

补链边全部 `confidence=medium` —— 与 sqlglot 直接解析出的高置信边区分;前端/L-4
可据 `confidence` 弱化展示。

### PR3 —— step-level 报告字段 + worker/前端消费

1. 移植 `report.py:308 _process_steps_from_segments` 语义:`procedure_segments`
   → `process_steps`(procedure_name/kind/segment_index/dml_keyword/line_start/
   line_end/preceding_comment/parse_status)。挂到 report(单文件)与批量 report。
2. `_collect_procedure_operations` 按 `line_start` 顺序产 target ops,喂 L-4
   `semantic.py` 的 refresh_mode 判定(修正 TRUNCATE→INSERT 顺序,复用 L-4 已有
   `classify_refresh_mode`)。
3. worker `_execute_lineage_batch` / `_lineage_batch_file` 透出 procedure_segments
   与 step 计数(与 L-4 `semantic_view` 挂载点同,`LineageBatchStatusResponse.report`
   为开放 dict,无需改 API schema)。
4. 前端 batch/single tab 增 step 视图卡片(读 `process_steps` + `dynamic_sql_segments`
   + `variables`)。**接后端页面按 CLAUDE.md §4 真走通验证**(浏览器 + F12 无红
   error + 真后端返回 procedure_segments)。

---

## 四、范围建议(首版必做 / 次版)

### 首版必做(高价值、低风险,纯静态无 DB)

- **G1 控制流感知切段** + **G7 PACKAGE/嵌套/匿名/TRIGGER 识别**:这是「深度解析」
  的地基,不做则大过程整块解析失败(2.0 现状:`DECLARE` 行直接让整脚本 sqlglot
  失败,退回裸分号切分,精度崩)。
- **G2 业务标题** + **G8 step-level 字段**:step 视图是 L-3 对用户可见的主产出。
- **G4 动态 SQL 告警**:至少 EXECUTE IMMEDIATE / PREPARE 两条精确路径 + unresolved
  占位 warning。诚实告警比静默丢失重要。
- **G5 变量抽取** + **G6 局部变量过滤**:G6 直接修正 tables 列表假表污染(正确性),
  必做;G5 前端面板,顺带。
- **G3 游标补链**:任务书明确点名的核心能力,首版含**内联 `FOR rec IN (SELECT…)
  LOOP`** 与 **LOOP 范围继承**(A4 的 1、3)。

### 次版(更启发式、命中率低、可增量)

- **A4-2 显式游标声明回填**(`CURSOR x IS …` + `FOR rec IN x LOOP`):跨声明区
  匹配,复杂度高、真实脚本占比低,次版补。
- **A7 UDF / BULK COLLECT / TRIGGER 补链**(PR2 后半):三者都是 `confidence=medium`
  启发式,命中场景少,可在 step 视图稳定后增量加,不阻塞首版。
- **动态 SQL 变量拼接重建**(`_rebuild_concat`,confidence=low):误报/精度权衡大,
  次版。
- **`_collect_procedure_operations` 喂 refresh_mode 排序修正**:依赖 L-4 已落地,
  作为 L-3×L-4 联调项放次版。

---

## 五、红线注意

- **R1(`app/services/` 禁 import DB 驱动)**:L-3 全部代码落 `app/domain/lineage/`,
  纯正则 + 字符串扫描 + sqlglot AST,**零数据库依赖**,天然满足。sqlglot 解析属
  domain 计算,不碰驱动。
- **R5(日志脱敏 / 禁 stdlib logging)**:segments/variables 若打日志走 structlog;
  但移植体本身是纯函数,建议**不加日志**(1.x 原实现也无),异常靠 `ignore_errors`
  容错,与 2.0 现有 `parse_errors` / `warnings` 通道汇报。
- **R10(禁 f-string 拼 uuid)**:不涉及。
- **无 R2/R3/R4/R8**:不碰密码/密钥/Fernet/hazmat/配置文件。
- **envelope 兼容**:`TableLineageEdge` 新增字段给默认值 None、`LineageReport`
  envelope `extra="allow"`,不破坏 1.x 兼容契约(`models.py:126` 注释所述)。

---

## 六、测试要求

现有 `tests/unit/test_lineage_parser.py` 已有 `test_plsql_split_rescues_insert_select_body`
(`:158`)等 plsql 用例,沿用其风格(内联 SQL 常量 + 断言 report 字段)。

**PR1 单测**(`tests/unit/test_lineage_segments.py` 新增):
- 控制流切段:`IF…THEN INSERT…END IF;` 只抽出 INSERT,壳子剥掉;嵌套 `BEGIN…END`
  内 `;` 不误切;`END LOOP;` / `END CASE;` 不当块级 END。
- 业务标题:`-- 集中交易\nINSERT…` → `preceding_comment="集中交易"`;Oracle
  hint `/*+ parallel */` 不当标题;截断 200 字符。
- PACKAGE BODY 多嵌套 proc 各出独立段;顶层匿名 `DECLARE…BEGIN…END` 出
  `procedure_kind=ANONYMOUS` 段;TRIGGER 抽 `trigger_source`。
- 动态 SQL:`EXECUTE IMMEDIATE 'INSERT…'` high;`EXECUTE IMMEDIATE v_unknown`
  → unresolved 占位 + `dynamic_sql` warning。
- 变量:`${p_dt}` / `:v` / `@s` 抽名 + `assigned_value`;PACKAGE 常量;
  `all_plsql_local_names` 过滤 `v_row` 不进 tables。
- line_start/line_end 与源码行号对齐(多行过程体)。

**PR2 单测**:
- 游标补链:`FOR rec IN (SELECT id FROM ods.b) LOOP INSERT INTO dwd.f VALUES
  (rec.id); END LOOP;` → 出 `ods.b → dwd.f` 边,`edge_type=CURSOR_LOOP_INSERT`
  / `confidence=medium`;已有 SELECT-derived 同边时不重复。
- UDF / BULK COLLECT / TRIGGER 各一最小用例,断言 edge_type 与源表→目标表。

**PR3 单测**:
- `process_steps` 字段完整、按 line_start 有序;`parse_status` 正确标 parsed/
  unsupported;dedupe 生效。
- 批量 report 挂载 procedure_segments,worker 单测断言透出。

**验证**(遵 CLAUDE.md §4):PR3 接后端页面须**真后端**跑通 —— 上传含存储过程的
脚本,批量分析返回真实 `procedure_segments` / `process_steps`,浏览器 F12 无红
error,贴响应体片段为证。

---

**未确认项**(移植前需实测,勿臆造):
1. 2.0 当前 sqlglot 版本对 Oracle `EXECUTE IMMEDIATE` / `BULK COLLECT` / PACKAGE
   BODY 的解析行为是否与 1.x 部署版一致(1.x 靠 `ignore_errors` 容错,2.0 需实测
   哪些段能过 qualify)。
2. 段 SQL 并入 `_analyze_statement` 后 `statement_index` 与顶层语句的对齐/去重
   策略(1.x 用 canonical 文本 dedup,`analyzer.py:170`)—— 2.0 需确认与现有
   `_append_unique` 不冲突,避免段与顶层重复计数。
3. `TableLineageEdge` 加 `edge_type`/`confidence` 后,前端 batch graph 渲染是否
   需同步(本设计假设向后兼容,字段可选)—— 待前端确认。
