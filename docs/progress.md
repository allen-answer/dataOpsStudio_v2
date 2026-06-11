# DataOpsStudio 2.0 进度看板(多 agent 同步)

> **用途**:同步"谁在做什么、做到哪了"。多 agent 并行(Codex / Claude 4.8 / review agent)
> 时的唯一状态源。
> **不替代** `docs/backlog.md`(债务项)和 `docs/agent-playbook.md`(踩坑案例 / 历史
> timeline)——本文件只管活动任务的分配与状态。

---

## 当前活动项

_(空 —— 2.0.0 GA 已发布,DM 已 Certified;无进行中任务。)_

> **2.0.0 GA 已发布**:回归走查 + 安全自审见 `docs/acceptance-2.0.0-ga.md`(10/10,
> 安全项全闭环/落档)。**DM 已 Certified**:真实例验证 8/8 + 修复 3 个 adapter bug,
> 见 `docs/acceptance-dm-certified.md`([#53](https://github.com/allen-answer/dataOpsStudio_v2/pull/53))。
>
> **后续候选(待人拍板,均无 owner/未排期)**:Oracle adapter(2.0.x)、外部安全审计
> 触发项(密码进 body 改造)、正式 license 签发(首个商业交付前)、worker DM 加密库
> 部署文档(首个 DM 客户前)、2.1.0 完整 SQL Workspace。

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

> **Wave 5 — 2.0.0 GA 准备(2026-06-11 全部合并)**:
> 5A 后端加固(Codex):pyjwt+pyotp 替换([#49](https://github.com/allen-answer/dataOpsStudio_v2/pull/49))、
> TOTP 同窗重放防护([#50](https://github.com/allen-answer/dataOpsStudio_v2/pull/50))、
> 错误码细分 + 残留小项([#51](https://github.com/allen-answer/dataOpsStudio_v2/pull/51))。
> 5B 前端(Claude 4.8):darkMode 键失配修复 + UX nit 包([#48](https://github.com/allen-answer/dataOpsStudio_v2/pull/48))。
> 5C(review agent):R3 条文对齐 + GA 四决策落档([#46](https://github.com/allen-answer/dataOpsStudio_v2/pull/46))、
> Compose CI hard-evidence job([#47](https://github.com/allen-answer/dataOpsStudio_v2/pull/47))。
> GA 回归走查 + 安全自审:`docs/acceptance-2.0.0-ga.md`(10/10 + 安全项全闭环/落档)。
>
> **Wave 4(2026-06-11 全部合并)— Part B 补全,PRD §1–§10 全部落地**:
> 4A 后端(Codex):revoked_tokens + force-logout([#39](https://github.com/allen-answer/dataOpsStudio_v2/pull/39))、
> §6 MFA + 改密([#40](https://github.com/allen-answer/dataOpsStudio_v2/pull/40))、
> §9 ai_configs + Gateway DB 配置 + L4 形态锁([#41](https://github.com/allen-answer/dataOpsStudio_v2/pull/41))。
> 4B 前端(Claude 4.8):登录第二因子 / §6 账户安全页 / §9 AI 配置页 / force-logout
> ([#42](https://github.com/allen-answer/dataOpsStudio_v2/pull/42),真手机验证器走查证据见 PR)。
> 走查揪出并修复:T4 潜伏 frozen-ApiError×contextmanager 500
> ([#43](https://github.com/allen-answer/dataOpsStudio_v2/pull/43) +
> [#44](https://github.com/allen-answer/dataOpsStudio_v2/pull/44) 补救)、登录第二步按钮塌缩(并入 #42)。
> 旁支:license sync 回归测试([#38](https://github.com/allen-answer/dataOpsStudio_v2/pull/38))。
>
> **2.0.0-skeleton(2026-06-11 打 tag + Release)**:contract §5 全量验收通过
> (报告 `docs/acceptance-2.0.0-skeleton.md`);compose 形态补齐
> ([#35](https://github.com/allen-answer/dataOpsStudio_v2/pull/35))。
>
> **Wave 3 开发项(2026-06-11 合并)**:T7 Part A 收口 —— 数据源编辑/删除 modal +
> 权限 8 开关面板 + Jobs 错误码精确映射 + 数据页宽屏流式布局
> ([#33](https://github.com/allen-answer/dataOpsStudio_v2/pull/33),Claude 4.8;
> daily-server 真后端 7 项走查证据见 PR)。
>
> **Wave 2(2026-06-11 全部合并)**:Datasource polish(PUT/DELETE + operation_policy +
> worker 结构化 error code,[#32](https://github.com/allen-answer/dataOpsStudio_v2/pull/32),Codex)、
> T7 Part B admin 三页 + License 管理([#30](https://github.com/allen-answer/dataOpsStudio_v2/pull/30),
> Claude 4.8)、AI Gateway 壳 + T6 部署尾巴([#29](https://github.com/allen-answer/dataOpsStudio_v2/pull/29),
> Claude 4.8)。
>
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
