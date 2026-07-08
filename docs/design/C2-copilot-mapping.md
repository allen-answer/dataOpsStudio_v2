# C2 — Compare 残余列映射学习(AI Copilot C2)

> 设计稿锚点:`docs/design/dataops-2.0-tech-design-v0.3.2.md` §2.7.4(C2,egress **L4 高风险**)、
> §2.7.5(AI Data Egress Policy,L4 语义)。Gateway 现状见 `docs/deployment/ai-gateway.md`。
> 本节是 1–2 页短设计,覆盖 C2 的数据流 / egress 边界(L2 + 可选 L4)/ 上下文取用与降级 / 审计。
>
> **提前交付**:设计稿原排 2.7.0(等历史沉淀),按用户指示提前。C2 与 2.2.0 规则推断
> (#72,同名/规范化相似列名规则映射)分工明确——**C2 只处理规则推断后的残余未映射列**,
> 不抢规则的活;历史为空时优雅降级,如实告知 AI "无历史",不假装有历史。

## 1. 目标

规则推断(#72)后仍未映射的**残余列**,由 AI 结合两侧 schema + 历史映射决策
(+ 可选样本)给出候选映射 + 置信说明。卖点是"上下文优势":AI 见过你以往怎么映射。
**只是建议,不自动应用**;前端逐条采纳,采纳 = 写入 `draft.columnMappings`(与规则
建议 applyMapping 同路径),执行仍走用户手动。

## 2. 端点

```
POST /api/projects/{project_id}/compare/ai-map-suggest
body: {
  source_id, target_id,                     # 两侧库侧 datasource
  source_table: {schema_name, table_name},
  target_table: {schema_name, table_name},
  confirmed_mappings: {src: tgt},           # 规则/用户已确认映射(据此剔除残余)
  include_samples: bool                      # L4 样本显式 opt-in,默认 false
}
```

## 3. 数据流

```
两侧 table ─┐
            ├─► 取 datasource(校验 project 访问,复用 compare/infer 路径)
            ├─► 取 AiGatewayRuntimeConfig(ai_configs 行 + SecretStore 取 key)
            │      └─ 未启用 / provider off ─► 结构化 409 ai_disabled
            ├─► L4 样本闸门:include_samples 且 l4_requires_optin ─► 403 l4_optin_required
            ├─► introspect 两侧列(metadata 缓存,仅结构)→ split_residual_columns
            │      └─ 任一侧残余为空 ─► ok=True 空建议(规则已覆盖,不浪费出站)
            ├─► 聚合历史映射决策(本项目既有 compare_tasks.compare_rules.column_mappings)
            ├─► 组上下文:残余列 schema(L2)+ 历史(L2)+ 样本(L4,仅 opt-in 且授权)
            ├─► 组 prompt(has_history / include_samples 分支;无历史如实告知)
            ├─► AiGateway.complete(purpose="compare_ai_map_suggest")
            │      └─ Gateway 内:egress 闸门 → 脱敏钩子 → provider → 审计
            ├─► parse_map_suggestions(只收残余列内的建议,绝不臆造列)
            └─► 审计 ai_assist_call,返回 {suggestions[], history_available, samples_included, ...}
```

## 4. Egress 边界(L2 默认,L4 需显式 opt-in + 管理员授权)

- **L2 层(默认恒送)**:残余列 schema(表名/列名/类型/可空/主键/注释)+ 历史映射决策
  (源列名→目标列名 + 确认次数)。**绝不含行值**——上下文构造只从 `Column` 与历史
  聚合取字段,物理上无从取到行内容。L2 出站需管理员放行(`max_auto_egress_level>=2`),
  否则 Gateway `EgressBlockedError` → 结构化 403 egress_blocked。
- **L4 层(样本,高风险,仅 opt-in)**:`include_samples=true` 是用户显式 opt-in;样本
  真正出站还需 **gateway 配置授权 L4**。本代码库里 L4 只有在 `l4_requires_optin=false`
  (管理员为本形态授权 L4 出站)时才过 `LevelEgressChecker`(`max_auto_egress_level`
  上限仅到 L3,L4 永不自动)。二者缺一即 **403 l4_optin_required**(说明要开什么):
  - hosted:管理员 `l4_requires_optin=false` 授权 + 用户勾 include_samples → 发样本。
  - portable / on-prem 默认:`l4_requires_optin=true` 保持 → **永不发样本**(对齐
    §2.7.5 portable `l4_allowed:false` 语义)。
  - 样本仅取**残余列**(不含主键/已映射列),两侧各 ≤20 行,单元格截断,过 Gateway
    脱敏钩子后作为独立 L4 `ContextItem` 送出。取数失败/策略禁 SELECT → 优雅降级为
    L2-only(`samples_included=false` + `sample_skip_reason`),不整体失败。

## 5. 历史为空的优雅降级(提前交付核心语义)

- 历史来源:聚合本项目既有 `compare_tasks.compare_rules.column_mappings`(去重 + 计确认
  次数)。为空 → `history_available=false`,prompt 走 `has_history=false` 分支,显式写
  "No historical mapping decisions are available … do not fabricate history",**不假装
  有历史**。前端在建议区提示"暂无映射历史,仅基于 schema 建议"。

## 6. 审计(R5)

- **业务审计**:`_audit_business(action="ai_assist_call", resource_type="compare_task",
  result=success/failed/skipped, detail={provider, suggestion_count, egress_level,
  samples_included, history_count})`。detail 只记**计数 / 等级 / 是否含样本**,
  **绝不含建议内容 / 样本值 / prompt 原文**(契约测试断言 detail 无 suggestions/prompt/content)。
- **Gateway 审计**:`AuditSink` 记 `prompt_hash + 长度 + token + egress_level`,
  绝不落 prompt / 响应原文(`hooks.AiUsageRecord` 已保证)。
- 样本查询失败只记 `type(exc).__name__`,driver 原始错误(可能含 SQL 片段)不透传。

## 7. 设计裁决项

1. **端点挂 project 而非 datasource**:C2 输入是"两侧数据源 + 两表",天然 project 级
   (compare/infer / suggest-tasks 同挂 project),复用 `_compare_datasource_rows_for_project`。
2. **L4 opt-in = include_samples(用户) AND `l4_requires_optin=false`(管理员)**:现有
   ai_configs 无独立 "l4_allowed" 字段,`l4_requires_optin` 是唯一 L4 闸门。缺任一 → 403。
   这使 portable 默认(True)永不发样本、hosted 管理员显式授权后才可发,符合 §2.7.5 语义。
3. **文件侧 schema 暂不支持**:与 compare/infer 一致,两侧需为库侧 table(introspection +
   样本都走 datasource adapter)。文件侧残余 schema 由前端直传是后续增强,本 PR 不含。
4. **建议不自动应用 / 不持久化**:即时返回,不建表、无迁移;采纳一条 = 写入本地 draft,
   与规则建议同路径,执行仍走用户手动 + SqlGuard。
5. **provider 只返建议、宽松解析**:`parse_map_suggestions` 容忍 ```json 围栏/散文,
   只收残余列内建议(绝不臆造列),MockProvider 回 "ok" → 空建议 + ok=True。
