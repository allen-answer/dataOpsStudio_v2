# C1 — Schema-Aware NL→SQL 生成(AI Copilot C1)

> 设计稿锚点:`docs/design/dataops-2.0-tech-design-v0.3.2.md` §2.7.4(C1,egress **L2**)、
> §2.7.5(Data Egress L0–L5)。Gateway 现状见 `docs/deployment/ai-gateway.md`。
> 本节是 1–2 页短设计,只覆盖 C1 的数据流 / egress 边界 / 上下文取用与截断 / 审计。

## 1. 目标

自然语言 → 用**真实 schema**(表名 / 列名 / 类型)生成只读 SQL + 简短解释。
卖点是"上下文优势":AI 知道你的库结构。C1 是"读当前状态"型,day-one 满血,
**不依赖历史沉淀**,也**不读任何样本数据 / 行值**(egress 上限严格 L2)。

## 2. 端点

```
POST /api/datasources/{datasource_id}/ai/sql-generate
body: {
  natural_language: str,          # 用户意图(必填,<=2000 字)
  schema_name?: str | null,       # 可选:限定单个 schema
  table_names?: string[]          # 可选:限定表(需配合 schema_name)
}
```

**为什么挂在 datasource 而非 project**(设计裁决项,见 §7):introspection 全家
(`/datasources/{id}/metadata/*`)都是 datasource 级,C1 复用同一套 schema 缓存,
挂 datasource 与之同构;datasource 唯一归属一个 project,访问校验经
`_datasource_for_current_user`(校验 project 成员/owner)完成,语义等价"project 级"。

## 3. 数据流

```
用户 NL ─┐
         ├─► 取 datasource(校验 project 访问)
         ├─► 取 AiGatewayRuntimeConfig(ai_configs 行 + SecretStore 取 key)
         │      └─ 未启用 / provider off ─► 结构化 409 ai_disabled(AiDisabledError 映射)
         ├─► 取 schema 上下文(metadata 缓存:表/列/类型,**仅结构,无行值**)
         │      └─ 截断:最多 N 表 / 每表 M 列,truncated 标记透传
         ├─► 组 prompt(dialect + schema JSON + NL)
         ├─► AiGateway.complete(purpose="ai_copilot_run", L2 context)
         │      └─ Gateway 内:egress 闸门(L2 需 max_auto>=2)→ 脱敏 → provider → 审计
         ├─► 拆 content 为 sql + explanation(```sql 围栏优先)
         └─► 写业务审计 ai_copilot_run,返回 {sql, explanation, provider, model, ...}
```

## 4. Egress L2 边界(硬约束)

- 送进 Gateway 的 `ContextItem` 一律 `egress_level=L2`:**只含表名 / 列名 / 类型 /
  可空 / 主键 / 列注释**。表注释 / 列注释属结构元数据(§2.7.5 L2 示例含"schema 结构")。
- **绝不带**:样本数据值、行内容、`SELECT` 结果、过滤条件里的字面量(那是 L3/L4)。
  上下文构造函数只从 `Column`(name/type/nullable/pk/comment)取字段,物理上无从取到行值。
- Gateway 的 `LevelEgressChecker` 是最终闸门:L2 内容只有在
  `DATAOPS_AI_MAX_AUTO_EGRESS_LEVEL>=2`(即管理员在 ai_configs 放行 L2)时才出站,
  否则 `EgressBlockedError`。调用方不自证清白,闸门在 Gateway。

## 5. Schema 上下文怎么取 / 截断

- **取**:复用 `_metadata_*` 缓存读路径(#65),不额外打库。
  - 给了 `schema_name` + `table_names` → 直接按名取这些表的列(最省)。
  - 只给 `schema_name` → 列该 schema 的表。
  - 都没给 → `_metadata_all_tables`(全 schema 表清单)。
- **截断**(`app/domain/ai_copilot.build_schema_context`,纯函数,可单测):
  - 最多 `MAX_TABLES`(默认 12)张表;超出丢弃并置 `truncated=True`。
  - 每表最多 `MAX_COLUMNS_PER_TABLE`(默认 60)列;超出截断并置 `truncated=True`。
  - `tables_used` 回传实际入 prompt 的表名,前端可见"AI 看了哪些表"。
- 截断理由:控制 prompt token + egress 面;宁可少送也不送样本数据。

## 6. 审计(R5)

- **业务审计**:`_audit_business(action="ai_copilot_run", resource_type="datasource",
  resource_id=datasource_id, result=success/failed/skipped, detail={provider, tables, truncated})`。
  `ai_copilot_run` 在设计稿审计白名单内(§2.5 Job kind / §2.7 审计动作)。
- **Gateway 审计**:`AuditSink` 记 `prompt_hash + 长度 + token + egress_level`,
  **绝不落 NL 原文 / 响应原文**(R5;`hooks.AiUsageRecord` 已保证)。
- NL 原文不进任何日志:端点不 `log(natural_language)`;失败只记 `type(exc).__name__`。

## 7. 设计裁决项

1. **端点挂 datasource 而非 project**:见 §2。任务书示例路径是 project 级
   (`如 …`),但 introspection 面是 datasource 级、frontend 手上只有
   `datasource_id`(list 响应不含 project_id),挂 datasource 最省且同构。
2. **AI 未启用返 409(不是 200 body error)**:任务书要求"结构化 4xx"。
   与 Compare 归因(200 + ok:false 优雅降级)不同——C1 是主动生成动作,
   禁用是配置态冲突,用 `409 ai_disabled` 让前端明确走"禁用提示"分支。
3. **sql/explanation 拆分走宽松解析**:优先取 ```sql 围栏,取不到则整段作 sql、
   explanation 置空。不强制 provider 返 JSON(MockProvider 回 "ok" 也能过)。
4. **不自动执行**:生成结果只插入编辑器,执行仍走用户手动 + SqlGuard,符合"外科手术"。
