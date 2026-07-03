# Compare / Lineage 体验优化方案(基于成熟产品调研)

> 2026-07-03,Fable 5。背景:用户反馈平台使用体验不佳,指示"调研成熟项目后优化前后端"。
> 两路并行调研(带来源与置信度标注,完整报告在会话记录,此处只留结论):
> - **Compare 对标**:Datafold Cloud / data-diff OSS / SQLMesh table_diff / dbt-audit-helper / GE / Soda
> - **Lineage 对标**:OpenMetadata / DataHub / Marquez / Atlan / sqllineage / dbt Explorer
>
> 范围:仅 Compare 与 Lineage 两域(SQL 工作台用户明确暂不优化)。
> 原则:优先"改呈现与交互"而非"造新引擎"——两域的后端内核(hashdiff/diff_profile/
> 子图 CTE)已强于多数对标,差距在**信息架构与交互密度**。

---

## 〇、已达标/已超标项(勿重复立项)

| 对标模式 | 我们的现状 |
|---|---|
| 列级血缘 opt-in(OpenMetadata 踩过默认开的坑) | `include_columns` 默认 false ✓ |
| 点节点重新聚焦(dbt Explorer refocus) | 子图点节点即换焦点重查 ✓ |
| 影响分析导出(Atlan/DataHub CSV) | L-5 xlsx 已含 impact sheet ✓(#106) |
| 推断边人工确认(调研结论:无一家把裸置信度画在边上,成熟做法是离散状态+人工确认) | inferred 虚线 + 待审核面板 confirm/reject(#103)**超过全部对标** ✓ |
| 主键/列映射自动推断(Datafold 连 PK 都手选) | infer 端点 pk_candidates + 映射置信度 ✓ 超标 |
| 采样降成本(Datafold 容忍度模式) | sample_quick_check(置信区间上界)✓ |
| 差异画像(常量偏移/时区检测) | diff_profile ✓ 独有 |

## 一、Compare 优化(P0 → P2)

### P0-C1 结果页信息架构重构:三层漏斗 + 四状态卡片
**出处**:SQLMesh 三段式(Schema→Row Counts→逐列匹配率)+ dbt-audit-helper 四状态词表。
**现状**:results tab 直接是 4 桶表格,用户第一眼看不到"哪层出的问题"。
**改法**(纯前端,数据全部已有):
1. results tab 顶部加**四状态卡片**(only_source/only_target/diff/same:计数+占比,固定色系),点卡片=下方明细按桶过滤(现有 bucket 切换换个形态)
2. 卡片下加**逐列匹配率条**:`diff_profile.columns[*].diff_rate` 已有 → 列名+横向条(绿→红),点列名跳该列明细。把 profile tab 的核心信息前置到 results
3. schema 警告(schema_policy warn 产物)置顶显示

### P0-C2 双值 cell "只看差异列"开关
**出处**:Datafold Values tab + SQLMesh #2645 反面教训(宽表平铺不可读)。
**现状**:diff 桶已有单元格分裂(#74),但全列平铺,宽表难读。
**改法**(纯前端):明细表默认只显示 `主键列 + cells 里出现过差异的列`,加"显示全部列"开关。

### P1-C3 差异行 SQL 逃生口
**出处**:GE `unexpected_index_query` + data-diff materialize。
**改法**(后端小件):结果页"复制差异行 SQL"按钮 —— 后端按 run 的 only_source/only_target/diff 桶主键生成 `SELECT ... WHERE (pk) IN (...)`(超过 N 个主键时生成带说明的截断版)。DBA 工作台的天然杀手细节。

### P1-C4 run 前主键健康预检
**出处**:SQLMesh grain 校验(NULL/重复主键警告)。
**现状**:主键选错时结果全是噪声,用户要跑完才发现。
**改法**(后端):`POST tasks/{id}/run` 前(或独立 precheck 端点)对两侧跑 `COUNT(*) vs COUNT(DISTINCT pk)` + pk NULL 计数,异常返回 409/警告(前端黄条 + 样本)。

### P1-C5 列下钻 NULL 语义拆分
**出处**:dbt-audit-helper 七态(perfect match / both null / null 单边 / 值冲突…)。
**改法**(后端 diff_profile 扩展):`_ColumnStats` 增加 null 侧别计数,前端列下钻用堆叠条形图。"不匹配"里一大半是 NULL 问题,拆开省一轮人肉 SQL。

### P2-C6 历史趋势 sparkline + 一键转定时
**出处**:Soda check history / Datafold "从结果 Create monitor"。
**依赖**:历史端点已有(#105);定时依赖 Workflow PR-4b(cron)。
**改法**:历史面板每任务差异行数迷你折线(前端,数据已有);"另存为定时对比"入口在 PR-4b 合并后接(生成含 compare 节点的 workflow + cron)。

## 二、Lineage 优化(P0 → P2)

### P0-L1 点边开抽屉露 SQL
**出处**:OpenMetadata 边详情(Source/Target/SQL)、Atlan process 节点+侧栏 SQL。
**现状**:边只有 title tooltip(transformation 一行字);"这条边为什么存在"要回 SQL 解析 tab 自己找。
**改法**(前后端小件):边表已有 `run_id` → 后端补 `GET /lineage/edges/{id}` 详情(含所属 run 的 source_ref + SQL 文本节选 + transformation);前端 SVG 边加宽透明 hit-area,点击开右侧抽屉(语法高亮 + 复制)。**这是两域全部优化里性价比最高的一条。**

### P0-L2 懒展开 + 计数徽章
**出处**:DataHub/Atlan/dbt Explorer 一致模式(默认 1 度 + `+N` 按钮按需展开)。
**现状**:一次画满 depth=3,中等图已显拥挤;换焦点是全量重查。
**改法**:后端子图响应给边界节点补 `hidden_upstream_count/hidden_downstream_count`(一个聚合查询);前端边界节点画 `+N↑/+N↓` 圆钮,点击=以该节点追加拉 1 度子图增量 merge 后重排。默认 depth 降为 2。

### P0-L3 图内定位搜索
**出处**:Atlan "Find in canvas" / OpenMetadata LineageSearchSelect。
**改法**(纯前端):画布左上搜索框,匹配已渲染节点 → 平移+高亮;未渲染 → 提示。

### P1-L4 每层节点上限 + "还有 N 个"聚合节点
**出处**:OpenMetadata Lineage Config + LoadMoreNode。
**改法**:子图 CTE 加 `nodes_per_layer` 上限(后端),溢出时前端画聚合占位节点,点击分页替换。防扇出 50 的表炸图。

### P1-L5 手动补边 + 归属标注
**出处**:DataHub 手动边+编辑者头像 / OpenMetadata 拖拽编辑。
**改法**:lineage_edges 增加 `origin: parsed|manual` + `created_by`(迁移);`POST /lineage/edges` 手动建边(校验表存在);前端手动边角标 + tooltip 显示操作人时间。解析不可能全对,补边闭环建立信任。

### P2-L6 布局器换 elkjs
**出处**:OpenMetadata/Marquez 同款(纯 JS、框架无关)。
**改法**:保留 SVG 渲染,仅用 elkjs 替换自研分层坐标计算(交叉减少、长边路由更好)。调研结论:几百节点内不必换渲染引擎(Vue Flow/G6 均不划算),布局器替换收益/成本比最高。

### P2-L7 单列聚焦视图
**出处**:DataHub 列 breadcrumb / Marquez 独立 column 路由。
**依赖**:column-trace API 已有(#104)。
**改法**:前端加"列追溯"视图(hop 分组列表 + 简化链图),从子图列节点右键进入。

## 三、实施顺序(建议)

| 批次 | 内容 | 体量 | 备注 |
|---|---|---|---|
| **UX-1(先做)** | P0-L1 边抽屉、P0-L3 图内定位、P0-C1 三层漏斗、P0-C2 差异列开关 | 中 | C 系前端等 Compare 收口 PR 合并后动 CompareView |
| **UX-2** | P0-L2 懒展开、P1-C3 差异 SQL、P1-C4 主键预检 | 中 | 前后端混合 |
| **UX-3** | P1-C5 NULL 七态、P1-L4 每层上限、P1-L5 手动补边(含迁移) | 中大 | |
| **UX-4** | P2 全部(elkjs / 单列视图 / 趋势+定时) | 大 | 定时项依赖 PR-4b |
