# DataOpsStudio 2.0 进度看板(多 agent 同步)

> **用途**:同步"谁在做什么、做到哪了"。多 agent 并行(Codex / Claude 4.8 / review agent)
> 时的唯一状态源。
> **不替代** `docs/backlog.md`(债务项)和 `docs/agent-playbook.md`(踩坑案例 / 历史
> timeline)——本文件只管活动任务的分配与状态。

---

## 当前 Wave:Wave 1(三线并行,目录零重叠)

| 任务 | 负责 | 分支 | 目录边界(铁律:不越界) | 状态 | PR |
|---|---|---|---|---|---|
| T5 License + Repair Mode + admin 路由 | Codex | `feat/t5-license-repair-mode` | `infrastructure/license/` + `api/` + `launcher.py` | 进行中(license 主体 + middleware 完成;admin endpoint 清单已人确认,路由实现中) | — |
| T8 migrate_from_v1 | Claude 4.8 ① | `feat/t8-migrate-from-v1` | `tools/` + `tests/`(**不动 app/**) | **已合并** | [#26](https://github.com/allen-answer/dataOpsStudio_v2/pull/26) |
| DM adapter + ColumnType 统一枚举 | Claude 4.8 ② | `feat/dm-adapter-columntype` | `dbclients/` + `domain/` + `worker.py` 仅 dispatch 一处 | PR 待审(review 已过,backlog 收尾已补) | [#25](https://github.com/allen-answer/dataOpsStudio_v2/pull/25) |
| 全部 PR review(R1–R10 + 接口一致性) | review agent(主会话) | — | — | 常驻 | — |

**Wave 1 冲突预埋点(已规避)**:
- `worker.py`:仅 DM adapter 任务可碰(dispatch 一处);结构化 error code 改造留 Wave 2
- `schemas.py`:仅 T5 可碰;operation_policy 留 Wave 2

## Wave 2(解锁条件:T5 合并)

| 任务 | 负责 | 依赖 |
|---|---|---|
| Datasource polish:`PUT/DELETE /datasources/{id}`(409 引用检查)+ operation_policy(8×allow_*)+ worker 结构化 error code | Codex | T5 合并(schemas.py 释放)+ DM adapter 合并(worker.py 释放) |
| T7 Part B 五页(§6 账户安全 / §7 用户 / §8 项目 / §9 AI 配置 / §10 审计) | Claude 4.8 ① | T5 admin 路由合并 |
| AI Gateway 接 1 provider 调通 + T6 尾巴(systemd unit / log rotate / TLS 文档) | Claude 4.8 ② | 无 |

## Wave 3(收口)

| 任务 | 负责 | 依赖 |
|---|---|---|
| T7 Part A 收口:编辑/删除 modal + 权限面板 + Jobs 错误码精确映射 | Claude 4.8 | Wave 2 datasource polish 合并 |
| SQL Workspace 按 ColumnType 染色 | Claude 4.8 | DM adapter + 枚举合并 |
| §5 验收 e2e 全量对照(contract §5:portable 解压 → … → migrate) | review agent + 人 | 全部合并 |

---

## 状态字典

`未开工` / `进行中` / `PR 待审` / `已合并` / `受阻(写明卡点)`

## 维护规则

1. **每个任务 PR 必须顺带更新本文件自己那一行**(状态 + PR 链接),与代码同 PR 提交。
2. 无代码改动的状态变化(如受阻、重新分配)由 review agent 单独发 docs PR 更新。
3. Wave 全部合并后,整段移入下方「已完成归档」,看板只保留活动项。
4. 历史 timeline 长期归档在 `docs/agent-playbook.md §1`,本文件归档段定期清空搬运。

---

## 已完成归档

> 截至本看板建立(2026-06-10),2.0.0 骨架已完成:Step 1 围栏、T1(PG 队列 + worker)、
> T2(MySQL adapter + spool)、T3(SecretStore 两层)、T4(API + middleware + 列元数据)、
> T6(launcher + 冷启动 CI + 原地升级 runbook)、T7 Part A(phase 1–2δ + §3 核验标,
> 已追平现有 5 组 endpoint 能力上限)。逐 commit 明细见 `docs/agent-playbook.md §1`。
