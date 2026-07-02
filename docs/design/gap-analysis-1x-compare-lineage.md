# 1.x → 2.0 功能差距测绘:Compare 与 Lineage

> 2026-07-02,Fable 5。方法:三路并行考古 —— 服务器 1.x 真源码(`~/dataops-studio/app/`,
> 只读)逐文件测绘 Compare 域与 Lineage 域;本仓 main(433f087)盘点 2.0 已实现面。
> 不靠记忆推测,每条带源码定位。用途:用户反馈"1.x 好多功能 2.0 没有,尤其数据对比
> 和血缘分析"的立项底稿。
>
> **结论先行**:2.0 的两域是"内核更强、外围更薄"—— 对比内核(库内哈希下推/递归
> hashdiff/差异画像)和血缘内核(qualify 列级精度/边持久化/缓存)都超过 1.x,
> 但 1.x 围绕内核长出的**输入形态、报告深度、历史管理、域间联动**大量未迁移。

---

## 一、Compare 差距矩阵

### 2.0 已有且更强(无需动作)

| 能力 | 说明 |
|---|---|
| 4 桶 diff 内核 | 2.0 递归 hashdiff + 库内哈希下推(DM/MySQL 黄金一致性真机验证),优于 1.x 拉全量内存归并 |
| 差异画像 diff_profile | 2.0 新增:列级差异率/常量偏移/时区/缺失键区间聚类,1.x 无 |
| 采样快检 + AI 归因 | 2.0 新增 |
| 列映射/主键/表对自动推断 | 2.0 新增(1.x 全手工) |
| 数值/日期归一化容差 | 两代都有 |
| Excel 导出 + 签名 token + 限额 + GC | 两代都有(#96 补齐) |
| 异步执行 + 取消 | 两代都有(2.0 走统一 job 队列) |

### 缺失 — P1(核心使用路径,建议 2.2.x–2.3.x 补)

| # | 1.x 功能 | 1.x 位置 | 2.0 现状 | 建议 |
|---|---|---|---|---|
| C-1 | **文件源对比**:Excel/CSV/Parquet 与 SQL 任意两两组合(上传+sheet/编码/表头行配置) | `app/models/compare.py:59-95`、`app/readers/` | 只支持库↔库 | 独立 wave:readers 层移植(Excel/CSV 优先,Parquet 次之),复用现 4 桶内核 |
| C-2 | **自定义 SQL 对比**:单 SQL(同段 SELECT 两库各跑)/双 SQL(两侧各写)模式 | `compare.py:135-192` | 只能表+列配置,不能写任意 SQL | CompareTask 加 sql_mode;单 SQL 模式对"同构库核对"是刚需 |
| C-3 | **历史记录管理**:run 列表/分页/按任务过滤/删除/多 run 合并导出一个多 sheet Excel | `app/api/history.py`、`HistoryView.vue` | 只能按 run_id 单查,无任务历史列表端点与页面 | 小件:GET tasks/{id}/runs + 历史页;合并导出复用 #96 通道 |
| C-4 | **数据预览**:不落任务先预览两侧前 N 行/列名(选主键/映射前先看数据) | `app/api/uploads.py:43-177` | 无 | 小件:preview 端点(≤200 行,复用 adapter 流式) |
| C-5 | **规则补齐**:ignore_columns / trim / 大小写不敏感 / 空串视为 NULL | `compare.py:12-19`、`engine.py:314-345` | CompareRules 仅 schema 策略/列映射/数值容差 | 小件:规范化层加 4 个开关 |
| C-6 | **任务复制**(clone) | `api/tasks.py` | 无 | 一个端点 + 前端按钮 |

### 缺失 — P2(联动/编排,归 2.4.0 Workflow 域顺延)

| # | 1.x 功能 | 1.x 位置 | 2.0 现状 | 建议 |
|---|---|---|---|---|
| C-7 | **workflow compare 节点参数化**:`source_sql_override`/`key_columns_override` + `${var}` 插值 → 一个任务模板批量跑 | `app/workflow/nodes/compare.py` | 节点 payload 静态 | 2.4.0 PR-4c:节点 payload 变量插值(when 变量基建已有) |
| C-8 | **trace_compare 血缘逐跳对比**:给定(表,字段)沿上游血缘每跳自动生成 compare 节点,打包 workflow 一键跑,定位"哪一跳把数据搞错" | `services/trace_compare.py`、`TraceCompareModal.vue` | 无(两域联动杀手锏) | 2.5.0 候选:依赖血缘列级链 API(L-8)+ C-7 |
| C-9 | **运行通知**:webhook / 企业微信 / 邮件 | `services/notifier.py` | 无 | 2.4.0 Workflow 收口项(run 终态钩子) |
| C-10 | **sensor 触发**:SQL 条件满足触发 workflow(数据到达触发)+ 冷却期 | `services/scheduler.py:55-70` | 无 | 2.4.x/2.5.0,排 PR-4b(cron)之后 |
| C-11 | SQL preflight 体检 + EXPLAIN 行数估算(advisory) | `services/sql_preflight.py` | 只有 sql_guard 只读硬校验 | 低优先;EXPLAIN 已有端点可复用 |
| C-12 | run 索引仪表盘(磁盘字节/峰值 RSS/guard 中止原因) | `api/history.py:43-104` | run_index 表已有(#71),无 dashboard 端点 | 低优先 |

### 1.x 也没有的(勿误立项)

行级修复 SQL 生成、历史趋势图、DDL 级表结构对比、对比任务直连 cron(1.x 定时必须包 workflow)。

---

## 二、Lineage 差距矩阵

### 2.0 已有且更强(无需动作)

| 能力 | 说明 |
|---|---|
| 列级解析精度 | 2.0 `optimizer.qualify(schema=...)`,1.x 的列归属命门在 2.0 已解 |
| 边持久化 + 子图 CTE | 2.0 PG 持久化(ADR-0019/0020);1.x 是内存 TTL 索引(自认过渡方案) |
| sql_hash 缓存 | 2.0 新增(parser_version 参与失效) |
| inferred 边 confirm/reject 状态机 | 2.0 新增(1.x AI 推断边只读) |
| 影响分析端点 | 两代都有 |

### 缺失 — P1(直接决定"好不好用",建议 2.3.x 收口 + 2.5.0 批量档)

| # | 1.x 功能 | 1.x 位置 | 2.0 现状 | 建议 |
|---|---|---|---|---|
| L-1 | **方言广度**:sqlglot 全方言 + 自动识别(不填自动猜) | `app/lineage/dialects.py` | 白名单仅 mysql/oracle/dm —— **postgresql adapter 刚上线但血缘不认 postgres** | ★ 2.3.x 立即补:白名单加 postgres(+db2 待 Certified),成本≈一行注册+测试 |
| L-2 | **多脚本批量分析**:多文件/ZIP 上传、每文件读写表、跨脚本依赖 script_edges、汇总数据流 | `app/lineage/batch_analyzer.py` | 单 SQL 粘贴 | 2.5.0 主件:批量是 1.x 用户的核心工作流 |
| L-3 | **存储过程深度解析**:PROCEDURE/PACKAGE 体按 DML 段切开(保留业务标题注释+行号)、`FOR rec IN(SELECT)` 游标补链、动态 SQL(EXECUTE IMMEDIATE)识别告警、脚本变量 `${var}`/PL/SQL 局部变量识别 | `app/lineage/segments.py`(1099 行)、`variables.py` | 仅 `split_plsql_statements` 简单拆分 | 2.5.0 主件:与 L-2 同 wave(1.x 语义逐条对照移植) |
| L-4 | **语义血缘视图**:目标表整合视图、刷新模式识别(DELETE+INSERT 全量重刷/append/merge)、表角色标注(target/intermediate/source_fact/remote_dblink + config/reference/dimension)、observations 人话观察、风险点 | `semantic.py`、`roles.py`、`aggregation.py` | 无 | 2.5.0:报告深度的差距大头 |
| L-5 | **报告导出**:JSON + 多 sheet Excel(12 类 sheet:总览/脚本/过程段/解析失败/动态SQL/数据流/跨脚本依赖/字段映射/风险/AI 推断) | `services/lineage_exporter.py` | 无任何导出 | 2.3.x:先复用 #96 导出通道出基础版(边/影响/parse_errors 三 sheet) |
| L-6 | **边 confirm/reject 前端入口** | (2.0 独有后端) | PATCH 端点已有,**前端零调用** | ★ 2.3.x 立即补:子图边上加确认/拒绝按钮(后端契约现成) |
| L-7 | **AI 增强(enrichment)**:整份报告的 AI 解读,严格只增不改;AI 配置管理页(provider/model/key Fernet 落盘 + step-up 认证) | `lineage_ai.py`(988 行)、`lineage_ai_config.py` | 只有 AI 兜底解析;AI Gateway 有但无 admin 配置 UI | 2.6.0 Copilot 域顺延(与 Copilot 共用配置面) |
| L-8 | **字段多跳追溯 API**:给定(表,字段)返回上下游字段链(hop/from 链式) | `api/assets.py` column-lineage | 列级子图有,链式形态无 | 2.3.x 小件:CTE 已有,加个输出形态;同时是 C-8 trace_compare 的前置 |

### 缺失 — P2(可视化/生态,独立排期)

| # | 1.x 功能 | 1.x 位置 | 2.0 现状 | 建议 |
|---|---|---|---|---|
| L-9 | **图可视化深度**:G6/Cytoscape 双引擎、schema 分组容器、PII/SLA/owner 徽章、业务分组 DAG、独立字段级血缘图 | `LineageGraphPanel.vue` 等 | 简单 SVG 分层 | 前端专项 wave(Opus 子代理);徽章依赖资产域(2.0 未立项) |
| L-10 | OpenLineage 标准事件输出(对接 Marquez 等) | `openlineage_emitter.py` | 无 | backlog:有外部 collector 需求时 |
| L-11 | schema 文件上传辅助解析(introspect 之外再给一条离线路) | `lineage_service.py` | datasource introspect 有,文件上传无 | 低优先 |
| L-12 | 血缘进统一搜索/资产详情反查 | `api/search.py`、`api/assets.py` | 无资产域 | 随资产域立项(未排期) |

### 1.x 也没有的(勿误立项)

全库自动扫描建索引(1.x 的 lineage_index 是 workflow run 产物的内存聚合,非扫库)、
A→B 路径查询、真正的跨源血缘图(DB Link 仅识别标注)、血缘定时扫描器(靠 workflow cron 间接实现)。

---

## 三、排期建议(待人拍板)

| 档 | 内容 | 版本建议 | 体量 |
|---|---|---|---|
| **立即(收口窗口)** | L-1 postgres 方言、L-6 confirm/reject UI、L-5 基础导出、L-8 字段链 API、C-3 历史页、C-4 预览、C-5 规则补齐、C-6 复制 | 2.3.x + 2.2.x 补丁 | 全是小件,1–2 个 PR 批 |
| **Workflow 域顺延** | PR-4b cron tick(已出任务书)、C-7 节点参数化、C-9 通知、C-10 sensor | 2.4.0–2.4.x | 中 |
| **批量血缘大档** | L-2 批量/ZIP、L-3 存储过程深度、L-4 语义视图 | **2.5.0 立项**(建议命名"Lineage 深度报告") | 大(1.x 对照移植,segments.py 1099 行) |
| **对比输入形态大档** | C-1 文件源(Excel/CSV/Parquet)、C-2 自定义 SQL 模式 | **2.5.0 或 2.5.x** | 大(readers 层) |
| **联动杀手锏** | C-8 trace_compare(依赖 L-8 + C-7) | 2.5.x | 中 |
| **Copilot 域顺延** | L-7 AI enrichment + AI 配置页 | 2.6.0 | 中 |
| **生态/可视化** | L-9 图升级、L-10 OpenLineage、资产域 | 独立拍板 | 大 |
