# L-4 语义血缘视图 设计稿

> 2026-07-06,Opus 4.8。方法:三路并行考古 —— 服务器 1.x 真源码(`~/dataops-studio/app/lineage/`,
> 只读)测绘 refresh/roles/aggregation/observations/risks;本仓 main 测绘 2.0 血缘域接入面。
> 每条带源码定位,不靠记忆推测。用途:L-4"语义血缘视图"实现底稿。
>
> **结论先行**:L-4 是 base_result 的**纯派生消费者**——目标表整合、角色、人话、风险都能从
> `target_summary + graph_edges + insert_mappings + warnings` 组合推导。唯一阻塞项是
> "DELETE+INSERT / TRUNCATE+INSERT 全量重刷"识别,需**先扩 parser 让 DELETE/TRUNCATE 落 target_summary**。

---

## 一、1.x 逻辑测绘结论(移植依据)

### 刷新模式(1.x `aggregation.py:380 _classify_refresh_mode`)

枚举 + 优先级短路(从上到下第一个命中即返回):

| 序 | 值 | 判定 |
|---|---|---|
| 1 | `truncate_insert` | 同作用域内 TRUNCATE 先于 INSERT(全量重刷)|
| 2 | `delete_insert` | 同作用域内 DELETE 先于 INSERT **且存在无 WHERE 的 DELETE**(全量重刷)|
| 3 | `delete_insert_partial` | 同作用域内 DELETE 先于 INSERT,但 DELETE 都带 WHERE(分区/增量)|
| 4 | `merge` | 仅 MERGE(无 insert/update)|
| 5 | `update` | 仅 UPDATE |
| 6 | `append` | 仅 INSERT |
| 7 | `mixed` | 有任意写但组合不纯 |
| — | `None` | 无任何写(纯清理)|

`_INSERT_FAMILY` = {INSERT, INSERT_OVERWRITE, REPLACE, CREATE_TABLE_AS, CREATE_OR_REPLACE_TABLE_AS}(temp CTAS 排除)。

### 表角色(1.x `roles.py:83 identify_table_roles`)

8 种,多挂 + 单 primary。结构角色(读写):`target`(只写)/`intermediate`(既写又读)/`source_fact`(纯读兜底)/`remote_dblink`(名带 `@dblink`);命名角色(schema 整段精确 or basename 词边界):`config`/`reference`/`dimension`/`filter`。

primary 优先级:`remote_dblink > intermediate > target > config > reference > dimension > filter > source_fact`。

命名规则:
- schema 整段精确(`_SCHEMA_KEYWORDS`):dim/dimension→dimension;ref/reference/lookup/dict/code(s)→reference;config/conf/cfg/settings→config;filter/exclude/blacklist→filter。
- basename 词边界正则 `(?:^|_)…(?:_|$)` IGNORECASE(`_NAME_RULES`):config(config/conf/cfg/setting/param…)、reference(ref/lookup/dict/code/enum/type)、dimension(dim/dimension)、filter(exclude/blacklist/exception/filter)。
- source_fact 是**兜底**(纯读且无命名角色时补),不是独立正则。

### observations(1.x `semantic.py:315`,8 条中文模板,顺序固定)

写入 N 张目标表 / 其中 N 张全量重刷(truncate_insert+delete_insert)/ N 张 MERGE / N 张中转表 / 业务分组 / N 个存储过程共 M 段 DML / N 张 DB Link 远端 / N 段动态 SQL。

### risks(1.x `semantic.py:363`,**仅 2 类**)

`parse_error`(level=high)、`dynamic_sql_low_confidence`(低置信→medium,置信越低风险越高)。**"全量重刷无备份 / 缺主键 / 循环依赖"1.x 均未实现** —— 2.0 可借新拿到的 refresh_mode 数据补(见三)。

---

## 二、2.0 接入面(本仓 main)

- **输入**:`analyze_sql_lineage(LineageParseRequest) -> LineageReport`(`parser.py:61`)。`target_summary` 每写操作一条 `TargetSummary(table, operation, refresh_mode=None, statement_index)` —— `operation` 已区分 insert/create/merge/update,`refresh_mode` **字段已预留恒为 None**,正是 L-4 要填的槽。
- **缺口 D1/D2/D3**:DELETE 落 else→`unsupported_statement` warning,不进 target_summary、无 WHERE 标记;TRUNCATE 全仓无处理。→ 全量重刷识别**必须先扩 parser**。merge/append/update 可从现有 operation 直接推导。
- **挂载点**:批量 report JSON 新增 `semantic_view` 顶层段(worker `_execute_lineage_batch` 组装)。`LineageBatchStatusResponse.report` 是开放 `dict[str,Any]` —— **无需改 API schema、无需迁移**。
- **worker 缺口 D7**:`_lineage_batch_file` 目前只回传计数 + 表名,`operation`/`has_where` 被丢 —— L-4 聚合前需透出。

---

## 三、2.0 实现设计(简单优先,不搬 1.x 跨过程复杂度)

**作用域简化**:2.0 批量每文件即一个脚本作用域。refresh_mode 按**单个 LineageReport 内 statement_index 顺序**判定 before_insert,不引入 1.x 的"至少一端顶层"跨过程规则(2.0 parser 已把 procedure 段展平)。

### 3.1 parser 扩展(缺口修复)

在 `_analyze_statement` 的 `report.statements.append` 之后**提前短路**(DELETE/TRUNCATE 无需 metadata/qualify):
- `exp.Delete` → `TargetSummary(table, operation="delete", has_where=<有无 WHERE>)`,不产血缘边。
- TRUNCATE(`exp.TruncateTable` / `exp.Command` 兜底)→ `TargetSummary(table, operation="truncate")`。

早短路同时覆盖 strict 与 lenient 两路,避免重复。

### 3.2 domain 层 `app/domain/lineage/semantic.py`(新文件)

纯函数,入参 `LineageReport`(或批量 `list[(source_file, LineageReport)]`):
- `classify_refresh_mode(ops_for_one_table) -> RefreshMode | None`
- `identify_table_roles(report) -> list[TableRole]`(移植 1.x roles 规则)
- `build_target_integration(reports) -> list[TargetIntegration]`(跨脚本聚合)
- `build_observations(view) -> list[str]`
- `build_risks(reports, view) -> list[SemanticRisk]`(1.x 2 类 + 新增"全量重刷"提示)
- `build_semantic_view(reports) -> SemanticView`(总入口)

模型见 `models.py`:`RefreshMode` / `TableRoleKind` / `TableRole` / `TargetIntegration` / `SemanticRisk` / `SemanticView`。`TargetSummary.has_where` 新增。

### 3.3 worker 集成

`_lineage_batch_file` 透出每 target 的 `operation`/`has_where`/`statement_index`;`_execute_lineage_batch` 调 `build_semantic_view` 生成 `report_payload["semantic_view"]`。

### 3.4 前端(后续 PR)

增强 batch tab:在 script_edges 区块旁加"语义视图 / 目标表整合"卡片,读 `batchReport.semantic_view`。不新增顶层 tab(与 subgraph/impact 的交互式查询心智割裂)。

---

## 四、分阶段落地

| 阶段 | 内容 | PR |
|---|---|---|
| P1 | parser DELETE/TRUNCATE 扩展 + `has_where` + parser 单测 | 本 wave |
| P2 | `semantic.py` domain 层 + 单测 | 本 wave |
| P3 | worker 集成 + 批量 report `semantic_view` 段 + worker 单测 | 本 wave |
| P4 | 前端 batch tab 语义视图渲染 + i18n | 后续 PR |

**未确认项**(移植前需实测,勿臆造):(1) sqlglot 对 Oracle `table@dblink` 的 AST 形态 —— `remote_dblink` 角色能否触发待验;(2) TRUNCATE 在当前 sqlglot 版本落 `TruncateTable` 还是 `Command`,实现需兜底两种。
