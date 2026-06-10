# DataOpsStudio 2.0 进度看板(多 agent 同步)

> **用途**:同步"谁在做什么、做到哪了"。多 agent 并行(Codex / Claude 4.8 / review agent)
> 时的唯一状态源。
> **不替代** `docs/backlog.md`(债务项)和 `docs/agent-playbook.md`(踩坑案例 / 历史
> timeline)——本文件只管活动任务的分配与状态。

---

## 当前 Wave:Wave 2

| 任务 | 负责 | 依赖 | 状态 | PR |
|---|---|---|---|---|
| Datasource polish:`PUT/DELETE /datasources/{id}`(409 引用检查)+ operation_policy(8×allow_*)+ worker 结构化 error code | Codex | 无(依赖已全合并) | PR 待审(review 已过,CI 绿;已并入 main 解决看板冲突) | [#32](https://github.com/allen-answer/dataOpsStudio_v2/pull/32) |
| T7 Part B admin 三页 + License 管理(§7 用户 / §8 项目 / §10 审计 + License 状态横条/上传) | Claude 4.8 ① | — | **已合并**(daily-server 真后端走查证据见 PR) | [#30](https://github.com/allen-answer/dataOpsStudio_v2/pull/30) |
| AI Gateway 接 1 provider 调通 + T6 尾巴(systemd unit / log rotate / TLS 文档) | Claude 4.8 ② | — | **已合并** | [#29](https://github.com/allen-answer/dataOpsStudio_v2/pull/29) |

> Part B 剩余两页(§6 账户安全 MFA / §9 AI 配置)+ force-logout **卡后端表/列**,
> 候选打包为 Wave 4「Part B 后端补全」,见 backlog 交叉项。

## Wave 3(收口)

| 任务 | 负责 | 依赖 | 状态 | PR |
|---|---|---|---|---|
| T7 Part A 收口:编辑/删除 modal + 权限面板 + Jobs 错误码精确映射 | Claude 4.8 | #32 合并 | 未开工 | — |
| §5 验收 e2e 全量对照(contract §5:portable 解压 → … → migrate) | review agent + 人 | 全部合并 | 未开工 | — |
| DM 真实例集成验证(backlog 高优先级,Certified 前必做) | review agent + 人 | 需持牌 DM 实例 | 受阻(等 DM 实例) | — |

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

> **Wave 1(2026-06-10 全部合并)**:T5 License + Repair Mode + admin 路由
> ([#28](https://github.com/allen-answer/dataOpsStudio_v2/pull/28),Codex)、
> T8 migrate_from_v1([#26](https://github.com/allen-answer/dataOpsStudio_v2/pull/26),Claude 4.8)、
> DM adapter + ColumnType 统一枚举([#25](https://github.com/allen-answer/dataOpsStudio_v2/pull/25),Claude 4.8)。
> Wave 2 前置:ColumnType 前端适配([#31](https://github.com/allen-answer/dataOpsStudio_v2/pull/31))。
>
> 截至看板建立(2026-06-10),2.0.0 骨架已完成:Step 1 围栏、T1(PG 队列 + worker)、
> T2(MySQL adapter + spool)、T3(SecretStore 两层)、T4(API + middleware + 列元数据)、
> T6(launcher + 冷启动 CI + 原地升级 runbook)、T7 Part A(phase 1–2δ + §3 核验标,
> 已追平现有 5 组 endpoint 能力上限)。逐 commit 明细见 `docs/agent-playbook.md §1`。
