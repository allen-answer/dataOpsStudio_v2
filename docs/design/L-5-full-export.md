# L-5+ 血缘完整导出(12 类 sheet)设计稿

> 2026-07-07,Opus 4.8(1M)。方法:两路考古 —— 服务器 1.x 真源码
> (`~/dataops-studio/app/`,**只读**)测绘完整导出;本仓 `main`(`fix/lineage-batch-tab-blank`)
> 盘点 2.0 血缘导出接入面 + 数据现成度。每条带 `文件:行号`,不靠记忆推测,拿不准处显式标注。
> 用途:L-5+ "血缘完整导出" 实现底稿。
>
> **结论先行**:
> 1. 1.x 的"完整版 12 类 sheet"= **批量导出** `write_lineage_batch_excel`
>    (`app/services/lineage_exporter.py:56`),而非当前 2.0 `/lineage/export` 对标的东西。
> 2. 两代导出跑在**两个不同数据面**:2.0 现有 3-sheet 导出读**持久化子图**
>    (项目级 `lineage_edges` 表,SQL walk);1.x 12-sheet 读**批量分析内存结果**。
>    L-5+ 的自然落点是**给批量作业(L-2)加一个完整 Excel 导出**,而非扩现有子图导出。
> 3. 阻塞点:2.0 worker 在序列化批量报告前 **`item.pop("_report")`**
>    (`app/worker.py:1068`),把每文件的 `procedure_segments / statements /
>    parse_errors / dynamic_sql_segments / insert_mappings / graph_edges` 富明细全丢了,
>    只留计数。**明细类 sheet(7 类)在补 worker 前无数据可填**。

---

## 一、1.x 12 类 sheet 逐一测绘

来源:`app/services/lineage_exporter.py:56 write_lineage_batch_excel(path, result)`。
`result` = `analyze_lineage_batch` 的完整 dict(`app/lineage/batch_analyzer.py:23`,顶层 `base_result` 在 `:186-219`)。
每文件明细字段(`procedure_segments/statements/parse_errors/dynamic_sql_segments/warnings`)由
`_split_per_file_details`(`:91`)从 `files[]` 拆出、每行注入 `file_name` 列后独立成 sheet。

| # | Sheet 名 | 数据源(result key) | exporter 行 | 主要列 | 备注 |
|---|---|---|---|---|---|
| 1 | **流程总览** | `summary`(单行)| `:58` | files/success_files/failed_files/read_tables/write_tables/table_edges/script_edges/dag_cycles/write_conflicts/warnings | 统计汇总,`batch_analyzer.py:206-217` 拼 |
| 2 | **脚本清单** | `files[]`(明细字段剥离后)| `:64-67` | file_name/statement_count/read_tables/write_tables/temp_tables/variables/status/error + `*_count`(过程段/语句/解析失败/动态SQL/警告 计数)| 大数组字段被 `_split_per_file_details` 换成 `_count`,原文移入 sheet 3-7;`batch_analyzer.py:86-107` |
| 3 | **过程段明细** | 每文件 `procedure_segments[]` | `:68-72` | file_name + procedure_name/procedure_kind/segment_index/line_start/line_end/preceding_comment/parse_status/sql | PL/SQL 过程段;每行带 file_name |
| 4 | **语句明细** | 每文件 `statements[]` | `:68-72` | file_name + statement_index/type/title | 顶层语句清单 |
| 5 | **解析失败明细** | 每文件 `parse_errors[]` | `:68-72` | file_name + statement_index/error_type/message/statement_type… | 未解析告警(逐文件)|
| 6 | **动态SQL明细** | 每文件 `dynamic_sql_segments[]` | `:68-72` | file_name + 片段字段 | 动态 SQL 兜底 |
| 7 | **脚本警告** | 每文件 `warnings[]` | `:68-72` | file_name + type/message | 逐文件脚本级警告 |
| 8 | **流程图分组** | `table_groups[]` | `:74-76` | target_table/source_tables/dependency_tables/files | 按目标表聚合(`batch_analyzer.py:514 _table_groups`);业务分组雏形 |
| 9 | **表级数据流** | `table_edges[]` | `:77` | source_table/target_table/edge_type(字段来源/条件依赖)/file_name/statement_index/source_columns/target_columns/confidence/reason | DAG 边;`batch_analyzer.py:117-150` |
| 10 | **跨脚本依赖** | `script_edges[]` | `:78` | producer_file/consumer_file/table | 文件 A 写 ∩ 文件 B 读;`batch_analyzer.py:281 _script_edges` |
| 11 | **字段映射** | `field_mappings[]` | `:79-81` | file_name + target_table/target_column/source_tables/source_columns/expression/confidence/statement_index… | 列级映射明细;`batch_analyzer.py:110-112` |
| 12 | **风险提示** | `warnings[]`(顶层聚合)| `:82` | file_name/type/message | 环依赖/写冲突/最终产物/外部源表/多写;`batch_analyzer.py:415-458` |
| (可选) | **AI兜底推断** | `ai_inferred.edges[]` | `:84-88` | AI 推断边 | 仅当 `ai_inferred` 存在 |

> 补充:1.x 还有一个**单脚本导出** `write_lineage_excel`(`:31`,8-10 sheet:
> 来源表/脚本变量/字段血缘/落表字段映射/JOIN/WHERE/GROUP_BY/UNION + 解析失败/动态SQL),
> 数据面是单脚本 `analyze_sql_lineage` 结果。**本设计聚焦 12 类批量导出**,单脚本导出作为
> 后续小项(2.0 `LineageReport` 已含全部所需字段,见 §四)。
>
> 关于"刷新模式 / 表角色 / 业务分组":**1.x 批量 Excel 里并无独立 sheet**——它们是 1.x
> 报告展示模型(`app/lineage/report.py`)/ UI 概念。2.0 的 **L-4 语义视图**已把这三者
> 材化为结构化数据(见 §三),故本设计把它们列为**超出 1.x 字面 parity 的增值 sheet**。

---

## 二、2.0 现状:3-sheet 子图导出

端点:`app/api/routes/core.py:2628 POST /projects/{id}/lineage/export`(同步生成 xlsx +
一次性下载 token,复用 sql/compare 的 artifact 存储 + 限额通道)。
拼 sheet:`_lineage_export_sheets`(`core.py:5202`);写盘:`write_xlsx_workbook`
(`app/infrastructure/result_export.py:67`,接受 `list[(sheet_name, list[Column], list[Row])]`,
内置公式注入防护)。

| Sheet | 数据源 | 列 |
|---|---|---|
| **table_edges** | `subgraph.edges`(edge_kind != column)| source/target/depth/direction/inferred/inference_status/confidence |
| **column_edges**(可选)| `subgraph.edges`(edge_kind == column)| source/target/transformation/subtype/inferred/confidence |
| **impact** | `impact.impacts` | node/depth/paths(链 `->` 拼)|

**关键**:数据来自 `_lineage_subgraph_response` + `_lineage_impact_response`,即**项目级持久化
子图**(`lineage_edges` 表,焦点 BFS,depth≤5)。这是**单数据源、焦点子图**视角,与 1.x
批量的"一次分析一堆脚本"视角**不同源**。所以现有 3-sheet **不是** 12-sheet 的子集,而是
**平行特性**,二者数据面不重叠。

---

## 三、数据现成度矩阵(12 类 × 2.0)

2.0 批量报告结构(`app/worker.py:1069-1083 report_payload`,落 `lineage_batch_report.json`
artifact,API `core.py:2874-2887` 回读):

```
{ file_count, parsed, failed, skipped{non_sql,too_large,over_file_limit},
  table_edge_total, column_mapping_total,
  files: [{ source_ref, status, run_id, table_edge_count, column_mapping_count,
            parse_error_count, lenient_statement_count, tables_written, tables_read }],
  script_edges: [{producer_file, consumer_file, table}],
  semantic_view: { targets[], table_roles[], observations[], risks[] } }   # L-4
```

L-4 语义视图(`app/domain/lineage/models.py:116 SemanticView`,`domain/lineage/semantic.py`
构建):`targets`=`TargetIntegration`(table/primary_role/roles/**refresh_mode**/counts/
source_tables/source_files/titles);`table_roles`=`TableRole`(table/roles/primary_role);
`observations`=人话串;`risks`=`SemanticRisk`(level/type/message)。

| # 1.x sheet | 2.0 有没有 | 从哪来 | 缺什么 |
|---|---|---|---|
| 1 流程总览 | ✅ 有 | `report_payload` 顶层(file_count/parsed/failed/skipped/*_total)+ `script_edges` 计数 | 字段口径与 1.x 不同(无 dag_cycles/write_conflicts;有 skipped 分类)。可直接出 |
| 2 脚本清单 | ✅ 有(字段位移)| `report_payload.files[]` | 1.x 的 statement_count/temp_tables/variables **无**;2.0 有 table_edge_count/column_mapping_count/parse_error_count/lenient_statement_count/tables_read/tables_written。核心可出 |
| 3 过程段明细 | ❌ 缺 | 在 `_report.procedure_segments`,**`worker.py:1068` pop 丢弃** | 需 worker 持久化 |
| 4 语句明细 | ❌ 缺 | `_report.statements`,同上 pop | 需 worker 持久化 |
| 5 解析失败明细 | ⚠️ 仅计数 | `files[].parse_error_count` 有;明细 `_report.parse_errors` 被 pop | 需 worker 持久化明细 |
| 6 动态SQL明细 | ❌ 缺 | `_report.dynamic_sql_segments`,被 pop | 需 worker 持久化 |
| 7 脚本警告 | ⚠️ 仅计数 | `files[].lenient_statement_count` 有;`_report.warnings` 明细被 pop | 需 worker 持久化明细 |
| 8 流程图分组 | ⚠️ 可派生 | 2.0 批量**不算** `table_groups`;但 `semantic_view.targets[].source_tables` 是等价的"按目标表聚合" | 用 targets 重构此 sheet(结构接近,列名要对齐)|
| 9 表级数据流 | ⚠️ 分裂 | 有 datasource 时落 `lineage_edges` DB(可 SQL 查);**批量报告 JSON 里只有 `table_edge_count`,无边明细**;无 DB 文本导入时**边完全不落**(`worker.py:1130-1164`)| 批量报告需带边明细;无 DB 场景需 worker 持久化 `graph_edges` |
| 10 跨脚本依赖 | ✅ 有 | `report_payload.script_edges[]` | 完全就绪 |
| 11 字段映射 | ⚠️ 仅计数 | `files[].column_mapping_count` 有;明细 `_report.insert_mappings` 被 pop;有 DB 时列边在 `lineage_edges` | 需 worker 持久化明细(或从 DB 列边取,仅 datasource 场景)|
| 12 风险提示 | ✅ 有(更强)| `semantic_view.risks[]`(level/type/message)+ `observations[]` | 口径优于 1.x(有 level 分级 + 人话观察)。可直接出 |
| (增值)表角色 | ✅ 新增可出 | `semantic_view.table_roles[]` | 1.x Excel 无此 sheet,L-4 材化后可增 |
| (增值)目标整合/刷新模式 | ✅ 新增可出 | `semantic_view.targets[]`(refresh_mode/counts/roles/source_files)| 1.x Excel 无,L-4 材化后可增 |
| (可选)AI兜底 | ⚠️ 有能力未接 | `app/domain/lineage/ai_fallback.py` 存在;批量报告未含 ai_inferred | 需确认批量是否跑 AI 兜底并落字段(**拿不准,标注**)|

**归纳**——两组:
- **A 组(数据已就绪,零 worker 改动)**:1 流程总览、2 脚本清单、10 跨脚本依赖、12 风险提示、
  8 流程图分组(用 targets 派生)、+ 增值 表角色 / 目标整合。共 **6-7 sheet**。
- **B 组(依赖 worker 补持久化)**:3 过程段、4 语句、5 解析失败明细、6 动态SQL、7 脚本警告、
  11 字段映射明细、9 表级数据流(边明细 / 无 DB 场景)。共 **7 sheet**,全部卡在
  `worker.py:1068 pop("_report")`。

---

## 四、落地设计

### 4.1 落点:批量作业新增完整导出,而非扩子图导出

1.x 12-sheet 是**批量分析结果**的导出。2.0 对应物是 `LINEAGE_BATCH` 作业 + 其
`lineage_batch_report.json` artifact。建议**新增**:

```
POST /projects/{id}/lineage/batch/{job_id}/export   # 完整 12(+2 增值)sheet
```

- 复用现有 xlsx 基建 `write_xlsx_workbook`(`result_export.py:67`)+ 一次性 token +
  限额 + GC 通道(与 3-sheet 子图导出、compare/sql 导出同源,**不新建下载链路**)。
- 新增一个纯函数式 exporter,如 `app/services/lineage_batch_export.py::batch_export_sheets(report) -> list[(name, Column[], Row[])]`,
  形态对齐 `_lineage_export_sheets`(`core.py:5202`)。**只读 report dict,不碰 DB / 驱动**(守 R1)。
- 现有 3-sheet `/lineage/export`(子图/影响)**保留不动**——它是平行特性,不合并。

> 备选:若产品要求把 12-sheet 挂到现有 `/lineage/export`,则该端点需能拿到批量 report。
> 但子图端点是项目级、无 job 上下文,强行合并会耦合两个数据面。**建议分开**(标注,待产品确认)。

### 4.2 分两个 PR

**PR1 — A 组 sheet(无需 worker 改动,数据已在 report JSON / semantic_view)**
- 新端点 + `batch_export_sheets` 覆盖:流程总览、脚本清单、跨脚本依赖、风险提示、
  流程图分组(targets 派生)、表角色、目标整合(刷新模式)。
- 纯 exporter + 端点 + 前端下载按钮。**可独立交付、独立验收**。

**PR2 — B 组 sheet(依赖 worker 补持久化明细)**
- 改 `worker.py:_execute_lineage_batch`:序列化前**不再整体 pop `_report`**,而是投影出一个
  **受控子集**落进 report JSON——每文件保留 `procedure_segments / statements /
  parse_errors / dynamic_sql_segments / warnings / insert_mappings / graph_edges`
  的**必要列**(见 §一列清单)。
  - ⚠️ **体积风险**:批量上限 500 文件 / 100MB(`worker.py:109-111`),全量明细可能撑爆
    report JSON。需**加封顶**(如每文件明细行数上限 + 超限落 `*_truncated` 标记),
    并复用 `write_xlsx_workbook` 的 `limit_bytes`(现 50MB,`core.py:216`)。
  - 无 DB 文本导入场景 `graph_edges` 当前不落(`worker.py:1162-1164`),表级数据流 sheet
    在该场景需 worker 显式带出内存边。
- exporter 追加:过程段/语句/解析失败明细/动态SQL/脚本警告/字段映射明细/表级数据流。

> PR1/PR2 可分离的根据:A 组数据**今天就在** `lineage_batch_report.json` 里,PR1 不依赖 PR2;
> B 组是纯增量(新增 sheet + worker 字段),不改 PR1 已交付 sheet 的行为。

---

## 五、范围建议(首版)

**首版 = PR1 的 A 组 7 sheet**,理由:
- 零 worker / 零数据面改动,风险最低,能立刻交付"批量分析可导出报告"这一用户价值。
- 覆盖用户最常要的**总览 + 清单 + 依赖 + 风险 + 分组 + 角色 + 刷新模式**(汇总/结构视角)。
- 明细类(过程段/语句/字段映射逐行)是"深挖单点"场景,优先级次之,放 PR2。

**次版 = PR2**:补 7 类明细 sheet,达成 12(+2)完整覆盖。**门槛是先定 worker 明细持久化的
封顶策略**(体积)——建议 PR2 拆成 PR2a(worker 持久化 + 封顶)+ PR2b(明细 exporter sheet)。

**AI兜底 sheet**:待确认批量流程是否实际产出 `ai_inferred` 字段(现 report JSON 未见),
**拿不准,先不排期**。

---

## 六、红线 + 测试要求

**红线**(`AGENT_CONTRACT_v2.md §4`):
- **R1**:exporter 放 `app/services/`,**禁 import 数据库驱动**;只吃 report dict / semantic_view,
  不自己连库。若 PR2 表级数据流要读 DB 列边,读取逻辑留在 `core.py` 端点层(已在 `conn` 上下文),
  不下沉到 exporter。
- **R5**:导出内容禁敏感值;SQL 文本片段(过程段 sql 列)入 Excel 前经 `_excel_safe_value`
  同类清洗(控制字符 / 超长截断,参考 1.x `lineage_exporter.py:138`),避免脱敏 processor 绕过。
- **R10**:run_id / 文件名生成禁 `f"...{uuid4().hex}"` 拼接——沿用 `new_id()`
  (现 `core.py:2668 export_job_id = new_id()`)。
- 下载走既有一次性 token + 限额,**不新开公网下载面**。

**测试**:
- 单测:`batch_export_sheets(report)` 对样例 report(含空/满/超长/缺 semantic_view)产出
  正确 sheet 数与列;超长 SQL 单元格截断;公式注入前缀防护(复用 `write_xlsx_workbook` 已有)。
- 集成:批量作业 success → 调导出端点 → openpyxl 回读校验 sheet 名集合与首行。
- PR2 追加:worker 明细持久化后 report JSON 结构断言 + 封顶生效(超限文件 `*_truncated`)。
- **验收证据**:真跑一次批量(launcher API + worker + PG),下载 xlsx,openpyxl 打开列出
  12(+2)sheet 名 + 抽样行,贴 stdout(遵 CLAUDE.md §4"接后端页面要真走通")。

---

## 附:关键源码定位速查

| 项 | 位置 |
|---|---|
| 1.x 批量导出(12 sheet)| `~/dataops-studio/app/services/lineage_exporter.py:56` |
| 1.x 单脚本导出(8-10 sheet)| 同上 `:31` |
| 1.x 明细拆 sheet | 同上 `:91 _split_per_file_details` |
| 1.x 批量结果结构 | `~/dataops-studio/app/lineage/batch_analyzer.py:186-219` |
| 1.x table_groups | 同上 `:514` |
| 2.0 现有 3-sheet 端点 | `app/api/routes/core.py:2628` |
| 2.0 sheet 拼装 | `core.py:5202 _lineage_export_sheets` |
| 2.0 xlsx 写盘基建 | `app/infrastructure/result_export.py:67 write_xlsx_workbook` |
| 2.0 批量端点(创建/查状态)| `core.py:2760` / `:2844` |
| 2.0 批量报告构建 + **_report pop** | `app/worker.py:986` / **`:1068`** |
| 2.0 批量报告结构 | `worker.py:1069-1083` |
| 2.0 每文件报告项 | `worker.py:1169-1183` |
| 2.0 L-4 语义视图模型 | `app/domain/lineage/models.py:116` |
| 2.0 语义视图构建 | `app/domain/lineage/semantic.py` |
| 2.0 LineageReport(1.x 兼容)| `app/domain/lineage/models.py:125` |
