# C-1 文件源对比 设计稿

> 缺口:2.0 Compare 域(#109)只支持 **库表↔库表 / 自定义 SQL↔SQL**(`CompareDataRef.kind ∈ {table, sql}`,两侧都必须绑数据源)。
> **不支持把一个 Excel / CSV / Parquet 文件当数据源与库表对比** —— 用户反馈的 "Compare 功能不全" 核心缺口(gap-analysis C-1)。
> 1.x 有完整的 `app/readers/` 抽象层;2.0 上传基建已落地(#112),`UploadPurpose` 甚至已预留 `compare_source` 值。
> 本稿只做源码考古 + 落地设计,**不改任何文件**。所有 1.x 引用为 `ssh daily-server:~/dataops-studio/`。

---

## 一、1.x readers 层测绘(移植依据)

### 1.1 统一契约:`RowReader` Protocol(`app/readers/base.py:5`)

所有源(SQL / Excel / CSV / Parquet)适配到同一 **dict-行** 形状喂给对比内核,只有两个方法:

```
fetch_all(*, max_rows, chunk_size, progress_callback) -> list[dict[str, Any]]
iter_rows(*, max_rows, chunk_size, progress_callback) -> Iterator[dict[str, Any]]
```

关键点:对比内核完全不感知源类型,只吃 `list[dict]` / `Iterator[dict]`。这是 C-1 能"复用现 4 桶内核"的地基。

### 1.2 各 reader 实现

| Reader | 文件/行 | 依赖 | 可配项 | 行为 |
|---|---|---|---|---|
| `SqlReader` | `sql_reader.py:9` | `dbclients.factory` | `datasource` + `sql` | 委托 `fetch_rows/iter_rows`,走 DB 驱动 |
| `ExcelReader` | `excel_reader.py:14` | **openpyxl** | `sheet`(空=第一个)、`header_row`(1-indexed) | `load_workbook(read_only=True, data_only=True)` 流式;公式取最后保存值;丢未命名列;跳全空行 |
| `CsvReader` | `csv_reader.py:12` | **stdlib csv**(不引 pandas) | `encoding`(默认 `utf-8-sig` 自动剥 BOM)、`delimiter`、`header_row`、`quotechar` | 逐行流;行长不齐补 None / 截尾;与 Excel 行为对齐 |
| `ParquetReader` | `parquet_reader.py:22` | **pyarrow**(懒加载) | `columns`(可选,仅读部分列) | `iter_batches(batch_size=chunk_size or 5000)`;列存 → **不保证主键有序** |

统一细节(三种文件源一致):
- **表头** 走 `app/services/compare_schema.py:12 uniquify_columns` —— 大小写不敏感去重,重名后缀 `__2/__3`,防 `dict(zip)` 覆盖。`normalize_column_name:8`。
- **列举列/表**:每个 reader 配套模块级函数 —— Excel `list_sheets(path)` + `list_columns(path, sheet, header_row)`;CSV `list_columns(path, encoding, delimiter, header_row)`;Parquet `list_columns(path)`(读 schema)。给"保存任务前先选列"的 UI 用。
- **类型处理**:无独立类型推断层。Excel `data_only` → 原生 python 类型(int/float/datetime);CSV → 全 str(空串 `""` 保留,由 `CompareRules.empty_as_null` 决定是否当 None);Parquet → pyarrow→python 原生类型。**规范化(trim / 大小写 / 数值容差 / 空值)全在对比内核的 `CompareRules` 做**,reader 只负责"取出原值"。
- **大文件**:全部流式读(openpyxl read_only / csv 行迭代 / parquet 分批);`max_rows` 硬顶(`RunLimits.max_rows` 默认 100k,`compare.py:26`)超出直接 `raise RuntimeError`;上传体本身另有 `MAX_UPLOAD_BYTES = 50MB`(`excel_uploads.py:16`),1MB 分块流式落盘,超限 413 不落盘。
- **懒依赖**:`parquet_reader._require_pyarrow()` 未装 pyarrow 时给明确中文错误,不在 import 期崩。

### 1.3 Parquet 的两条限制(移植时注意)

`parquet_reader.py:22` docstring 明写:列存按行迭代无序,**与 `stream_compare` 互斥**(1.x `_validate_compare_inputs` 强制 stream_compare 两侧都 SQL)。→ 文件源天然走"全量物化 + 内存对比",不走流式按键归并。

---

## 二、文件源如何接入对比(1.x 数据流)

### 2.1 数据模型(`app/models/`)

- `common.py:19 SourceKind = {sql, excel, csv, parquet}`;`SqlMode = {single, double}`。
- `compare.py:59 CompareTask` —— **per-side 字段惯例**(compare.py:53-58 注释):
  - SQL 端:`source_id` + `source_sql`
  - Excel 端:`source_excel_path` + `source_sheet` + `source_header_row`(Excel 特有 sheet)
  - CSV/Parquet 端:`source_file_path`(通用路径);CSV 另加 `source_file_encoding` + `source_csv_delimiter` + `source_header_row`;Parquet 自描述不用这些
  - `target_*` 完全对称
- 校验 `_validate_compare_inputs`(compare.py:132):per-side 必填校验 + 两条 strict 跨字段规则 —— **single SQL 模式禁止任一侧文件源**(单 SQL 是同段 SELECT 两边跑,文件跑不了)、**stream_compare 要求两侧 SQL**。文件源对比 = double 模式 + 非流式。

### 2.2 reader 装配:`build_reader(task, side)`(`app/services/runner.py:536`)

按 `task.{side}_kind` 分派:
- `SQL` → 取 datasource + SQL(single 模式 target 复用 source_sql)→ `validate_readonly_sql` → `SqlReader`
- `EXCEL` → `resolve_excel_path(...)` → `ExcelReader(path, sheet, header_row)`
- `CSV` → `resolve_uploaded_path(..., allowed_suffixes={.csv,.tsv,.txt})` → `CsvReader(path, encoding, delimiter, header_row)`
- `PARQUET` → `resolve_uploaded_path(..., {.parquet,.pq})` → `ParquetReader(path)`

对比 runner(runner.py:273)对两侧统一 `build_reader → fetch_all/iter_rows`,**下游内核对源类型无感**。

### 2.3 路径安全:`resolve_uploaded_path`(`app/services/excel_uploads.py:68`)

任务里存的是 **相对仓库根** 的路径;resolve 时 `(RESULTS_DIR.parent / stored).resolve()` 必须落在 `UPLOADS_DIR` 内(路径穿越防护)+ 后缀白名单 + 存在性检查。

### 2.4 上传 & 预览端点(`app/api/uploads.py`)

- `POST /api/uploads/excel|csv|parquet`(uploads.py:187-199)→ `excel_uploads/file_uploads` 流式落盘 → 返回 `{path, filename, sheets/columns_by_sheet 或 columns, encoding, delimiter}`。CSV 有 **GBK 回退**(utf-8 失败自动试 gbk,`file_uploads.py:48`)。
- `POST /api/preview/columns`(uploads.py:109)+ `POST /api/preview/rows`(uploads.py:43)—— 接 `kind` + 文件参数,**不需已存任务** 就返回列名 / 前 N 行,给"选主键 / 忽略列 / 列映射"的 UI 用。

### 2.5 前端(1.x Vue,`frontend/frontend/src/`)

- 向导:`WorkbenchView.vue` → StepSource / StepRules / StepMapping / StepResult。
- **`views/workbench/DataSourcePanel.vue`**(side=source/target)是核心:4 选一 toggle **SQL / Excel / CSV / Parquet**(panel.vue:66-95);
  - Excel:file input `.xlsx/.xlsm` → 上传后自动列 sheet 下拉 + header_row 输入
  - CSV:file input `.csv/.txt/.tsv` → delimiter 下拉 + encoding + header_row
  - Parquet:file input,schema 自动识别
  - `uploadExcel/uploadCsv/uploadParquet`(taskStore action)POST 上传端点,把返回 path/columns 写进 `taskDraft.{side}_*`,并驱动预览表。

---

## 三、2.0 接入面(本仓 main,只读现状)

### 3.1 Compare 域是"分段哈希 diff 引擎"(与 1.x 差异大)

`app/domain/compare.py`:
- `CompareSegmentReader` Protocol(compare.py:190)—— 契约比 1.x 重:`segment_fingerprint(segment)`、`fetch_rows(segment)`、`bounds()` 等,面向**按主键分段 + DB 侧哈希下推**。
- `CompareHashExecutionMode = {db_hash, client_row_hash}`(compare.py:22);`recursive_hashdiff`(compare.py:217)递归二分对比;4 桶 `CompareDiffBucket`(only_source/only_target/diff/same)。
- **已有内存对比路径**:`_StaticRowReader`(compare.py:507)+ 内存 compare 助手(compare.py:~336-358,把 row 列表 normalize → `_StaticRowReader` → `recursive_hashdiff(recursive_checksum=False)`)。**这正是文件源可复用的落点**。

### 3.2 源/目标模型:只有 table / sql(**无 file**)

`app/api/schemas.py:137 CompareDataRef`:`kind ∈ {table, sql}` + `schema_name/table_name/sql`。`CompareTaskCreateRequest`(schemas.py:182)两侧都要 `source_id`/`target_id`(datasource)。→ **这是需要开的口子**。

### 3.3 Worker 执行:`_execute_compare_run`(`app/worker.py:720`)

- 两侧 `_required_payload_str(source_id/target_id)` → `_datasource_loader` → adapter → **`_DatabaseCompareReader`**(worker.py:2087)。
- `_DatabaseCompareReader` 实现 `CompareSegmentReader`:`bounds` / `segment_fingerprint`(**db-hash 是 opt-in**,`_can_use_db_hash` 仅 MYSQL/DM + 单键 + table ref,否则回退 = fetch 全行 + 客户端 `sum(row_hash64)`)/ `fetch_rows` / `fetch_all` / `fetch_first_at_or_after` / `fetch_key`。
- **关键**:客户端哈希回退路径已存在。文件源天然映射到这条 —— 无 DB、无下推、无分段,全量 fetch + 客户端 row hash。

### 3.4 上传基建(#112)已就绪,且预留了 compare

- `POST /projects/{pid}/uploads?purpose=&filename=`(core.py:1941)—— raw octet-stream body 流式 → `result_store.put_upload_artifact` → `uploads` 表登记(所有权/项目/purpose/storage_uri),超 `upload_max_mb` → 413。返回 `upload_id` + `storage_uri`。
- **`UploadPurpose = Literal["lineage_batch", "compare_source"]`**(schemas.py:433)—— `compare_source` **已预留**,DB check 约束也含它(models.py:637)。**无需新迁移**。
- Worker 回读:`result_store.open_download(ResultRef(backend="local_fs", uri=storage_uri))`(worker.py:1017,lineage batch #114 的现成消费范式)。
- 参考先例:lineage 批量 #115 已把 "datasource 可选(无库文本导入)" 打通 —— 让某侧不绑数据源的模式,worker 侧有前例可抄。

---

## 四、2.0 落地设计(简单优先,复用 4 桶内核)

### 核心决策

1. **新增 `CompareDataRef.kind = "file"`**,带 `upload_id` + 格式与解析参数。文件侧 `source_id/target_id` 变可选(照抄 #115 数据源可选范式)。
2. **新增 `app/domain/readers/` 纯 domain 层**,从 1.x 移植 Excel/CSV/Parquet reader + `uniquify_columns`。**不搬 `SqlReader`**(它碰 DB 驱动,留在 worker/db 层),从而 domain 层零 DB 依赖(R1 安全)。
3. **文件源强制走客户端哈希 + 全量物化**:worker 侧新增 `_FileCompareReader`,从 upload artifact 读行 → 复用 `_StaticRowReader` / 内存 compare 助手(3.1)喂 `recursive_hashdiff`。**不下推、不分段、stream_compare 关**(与 1.x 语义一致,也解决 Parquet 无序问题)。

### 分 PR 建议

**PR1 — readers domain 层(纯函数,可独立合)**
- 新 `app/domain/readers/{base,excel,csv,parquet}.py` + `uniquify_columns`(或复用现有 schema 归一)。
- 契约收敛到 2.0 需要的:`iter_rows()` / `fetch_all(max_rows)` + `list_sheets` / `list_columns`。
- 纯单测(样例文件 → 行/列/sheet/编码/表头),不碰 API/worker。**openpyxl / pyarrow 加依赖**(pyarrow 建议 optional-extra + 懒加载,照抄 1.x `_require_pyarrow`)。

**PR2 — 文件预览 API**
- 扩 `/projects/{pid}/compare/preview`(或新 `/compare/file-preview`)接 `upload_id` + 格式参数 → 校验 upload 归属(项目 + purpose=compare_source)→ `open_download` → reader → 返回 `{sheets?, columns, sample_rows, truncated, detected_encoding?}`。
- 给前端"上传后选 sheet/编码/表头 + 预览 + 选主键/列映射"用。CSV 保留 GBK 回退。

**PR3 — schema + 持久化**
- `CompareDataRef` 加 `kind="file"` 分支:`upload_id` + `file_format(excel|csv|parquet)` + `sheet` / `header_row` / `encoding` / `delimiter`(按格式条件必填)。
- `CompareTaskCreate/Update` 校验:文件侧免 `source_id`;移植 1.x 两条互斥规则思路(文件源 → 非流式;`table` ref 语义只对 DB 侧)。
- task 存 `upload_id`(不存原始路径,比 1.x 相对路径更干净,天然规避穿越)。

**PR4 — worker 执行接入**
- `_execute_compare_run`:`source_id/target_id` 改按 ref.kind 条件必填;某侧 kind==file 时 → 从 uploads 表取 storage_uri → `open_download` → 落临时文件/流 → 建 `_FileCompareReader`(内部 domain reader,`max_rows` 上限物化,算 `row_hash64`)→ 走内存 compare 助手。
- db-hash / 分段路径仅两侧都 DB 时启用;任一侧文件 → 全量客户端哈希。
- 清理临时文件;沿用 `RunLimits` 磁盘/超时护栏。

**PR5 — 前端 CompareView**
- 每侧源类型 toggle:table / sql / **file**;file → 复用 #112 上传 client(`fetch(blob)` + `purpose=compare_source`)→ 拿 `upload_id`。
- 按格式渲染 sheet 下拉 / 编码 / 分隔符 / 表头行(抄 1.x `DataSourcePanel.vue` 交互),调 PR2 预览端点渲染样例 + 供选主键/列映射。

---

## 五、范围建议

| 格式 | 版本 | 理由 |
|---|---|---|
| **CSV** | 首版 | stdlib、零重依赖、覆盖面最广、GBK 回退对国内 ETL 刚需 |
| **Excel** | 首版 | openpyxl 稳定、`.xlsx/.xlsm` 是用户最常拿来对的手工台账;需 sheet/header_row 交互但成本可控 |
| **Parquet** | 次版 | 需 pyarrow(重依赖,建议 optional-extra + 懒加载 + capability 探测);受众窄;列存无序但本设计已强制非流式故不额外受限 |

首版 CSV+Excel 即可覆盖用户反馈主场景;Parquet 单独 PR(PR1 里 reader 可一起移植但前端/依赖装配后置)。

## 六、红线注意(R1–R10)

- **R1(`app/services/` 禁 import DB 驱动)**:readers 放 `app/domain/readers/`,只依赖 openpyxl / csv / pyarrow,**绝不 import dbclients**;`SqlReader` 不移植进 domain。domain 层保持零 DB 依赖。
- **R5(日志禁敏感值 / 强制 structlog)**:文件内容可能含 PII(数据而非机密),但预览/错误日志**不整行打印数据**;沿用 structlog 脱敏 processor,禁 stdlib logging。
- **R3 / R8**:readers 不碰 Fernet/bcrypt/hazmat,不引入任何明文机密;上传走既有 `uploads` 表 + result_store,无新增密码/token 面。
- **R10**:新 id 用现成 `new_id()`,禁 f-string 拼 uuid。
- **护栏**:上传体 `upload_max_mb`(413)、行数 `RunLimits.max_rows`、磁盘配额/超时沿用现有;文件全量物化前先按 `max_rows` 截断,防 OOM(移植 1.x 硬顶语义)。

---

## 待确认

- 内存 compare 助手的确切函数名(domain/compare.py ~336)—— 已确认其形态(source_rows/target_rows → `_StaticRowReader` → `recursive_hashdiff`),接入前对齐签名。**未确认**是否已导出可直接复用,或需在 worker 侧薄封装。
- `result_store.open_download` 对上传件是否返回可 seek 流(openpyxl 需 seek;Excel 可能要先落临时文件再 `ExcelReader`)。lineage batch 用它读 ZIP(zipfile 需 seek)已跑通,**大概率 OK**,PR4 落地时验证。
- pyarrow 依赖装配策略(optional-extra vs 默认)由发布口径定,本稿建议 optional。
