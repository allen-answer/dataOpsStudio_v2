# C4 — 慢 SQL 根因诊断(AI Copilot C4)

> 设计稿锚点:`docs/design/dataops-2.0-tech-design-v0.3.2.md` §2.7.4(C4,egress **L3**)、
> §2.7.5(Data Egress L0–L5)。Gateway 现状见 `docs/deployment/ai-gateway.md`。
> 本节是 1–2 页短设计,只覆盖 C4 的数据流 / egress 边界 / 上下文取用与降级 / 审计。

## 1. 目标

给一条慢 SQL,AI 据 **执行计划 + 表结构/索引 + 历史运行基线** 排序最可能的根因
(缺索引 / 全表扫 / 统计过期 / join 次序 / 非 sargable 谓词 / 大中间结果等)并给
可执行建议。C4 是"读历史"型能力,原排 2.7.0(等运行基线积累)。

**提前交付**:按用户指示在基线尚浅时先上。核心是**优雅降级**——基线为空时不空转、
不臆造,prompt 如实告知"无历史基线",AI 只据 plan + 结构给建议。

## 2. 端点

```
POST /api/datasources/{datasource_id}/ai/slow-sql-diagnose
body: {
  sql: str,                    # 慢 SQL(必填,<=20000 字,只读 SELECT/WITH)
  explain_job_id?: str | null  # 可选:一个已完成的 sql_explain job,复用其执行计划
}
```

**为什么挂 datasource**:与 introspection 全家(`/datasources/{id}/metadata/*`)、C1
同构;datasource 唯一归属一个 project,访问校验经 `_datasource_for_current_user`。

## 3. 数据流

```
慢 SQL ─┐
        ├─► 取 datasource(校验 project 访问)+ 只读 SQL guard(非 SELECT/WITH → 400)
        ├─► 取 AiGatewayRuntimeConfig(未启用 / provider off ─► 结构化 409 ai_disabled)
        ├─► 执行计划(可选):explain_job_id 给了且是本 project 已成功的 sql_explain
        │      → 读其结果集为 plan(L1);否则跳过(plan_included=false,不阻塞 job 队列)
        ├─► 表统计:sqlglot 抽 SQL 引用表 → 元数据缓存取列 + 索引(L2,取不到的表跳过)
        ├─► 历史基线:jobs 聚合同一 sql_hash 近期成功 sql_query 的 duration(L1)
        │      → count/avg/min/max/p95;count=0 → available:false(如实降级)
        ├─► 组 context(SQL[L3,遮蔽字面量] + 结构[L2] + plan[L1] + 基线[L1])+ prompt
        ├─► AiGateway.complete(purpose="ai_slow_sql_diagnose")
        │      └─ Gateway 内:egress 闸门(L3 需 max_auto>=3)→ 脱敏 → provider → 审计
        └─► 写业务审计 ai_assist_call,返回 {diagnosis, plan_included, baseline_*, ...}
```

## 4. Egress 分层(硬约束,§2.7.5)

| 出站项 | 等级 | 说明 |
|---|---|---|
| SQL 原文 | **L3** | 出站前 `mask_sql`(复用 #83 `redact_sql_literals`)遮蔽字符串/数字字面量,保留结构与表列名,抹掉业务过滤值(那已逼近 L4 样本值) |
| 执行计划(EXPLAIN) | L1 | 算子 / 代价 / 行估,不含行值 |
| 表结构 + 索引 | L2 | 表名 / 列名 / 类型 / 索引定义,无行值 |
| 历史基线 | L1 | duration 聚合数字,纯统计量 |

context 最高等级 = **L3**。Gateway 的 `LevelEgressChecker` 是最终闸门:L3 只有在管理员
把 `max_auto_egress_level` 放行到 >=3 时才出站,否则 `EgressBlockedError`(端点降级为
`ok:false, error:"EgressBlockedError"`)。调用方不自证清白,闸门在 Gateway。

## 5. 上下文怎么取 / 降级

- **plan**:复用调用方已跑完的 explain job(前端 Plan tab 手上就有 `planJobId`),读其
  result_set 为 plan。**不同步等/轮询 job 队列**(见 §7 裁决)。
- **表统计**:`extract_table_refs` 用 sqlglot 抽表(宽松,解析失败→无表统计);逐表走
  `_metadata_*` 缓存(#65)取列 + 索引,取不到的表 try/except 跳过 → 降级。上限 `MAX_TABLES`。
- **基线**:`jobs` 聚合 `EXTRACT(epoch FROM finished_at-started_at)`,过滤
  `kind=sql_query AND status=success AND project_id AND datasource_ids @> [ds] AND
  payload->>'sql_hash'=:hash`。**不建新表、不落 SQL 原文**,只出聚合数字。

## 6. 审计(R5)

- **业务审计**:`_audit_business(action="ai_assist_call", resource_type="datasource",
  result=success/failed/skipped, detail={provider, tables, plan_included, baseline_runs, truncated})`。
- **Gateway 审计**:`AuditSink` 记 `prompt_hash + 长度 + token + egress_level`,**不落 SQL / plan 原文**。
- SQL / plan 原文不进任何日志:端点不 log SQL;失败只记 `type(exc).__name__`。

## 7. 设计裁决项

1. **plan 走"复用已完成 explain job",不同步等/轮询**:同步端点阻塞在 job 队列上体验差
   且复杂;前端 Plan tab 手上已有刚跑完的 explain job_id,带上即可。不带 → 降级为无 plan。
2. **AI 未启用返 409**(与 C1 一致):禁用是配置态冲突,用 `409 ai_disabled` 让前端走禁用
   分支;gateway 失败 / egress 拦截返 `200 ok:false`(优雅降级,不阻断)。
3. **审计 action 用 `ai_assist_call`**(任务书指定)而非 C1 的 `ai_copilot_run`:两者都在
   JobKind 内;按任务书取 `ai_assist_call`。若后续要与 C1 统一,改一处常量即可。
4. **masked SQL 仍归 L3**(而非按"已遮蔽→L2"降级):忠实设计稿 C4=L3 分类;遮蔽是纵深
   防御(即便 L3 也不漏真实过滤值)。因此本能力需管理员放行 L3 才可用,符合 §2.7.5。
5. **只读 guard**:diagnose 只诊断只读 SELECT/WITH(与 explain 一致,且保证 sql_hash 与
   历史 sql_query 口径一致,基线才对得上)。
6. **p95 用 `percentile_cont(0.95) WITHIN GROUP`**:PG 原生,单条聚合查询拿全分布摘要。
