# DataOpsStudio 2.0 进度看板(多 agent 同步)

> **用途**:同步"谁在做什么、做到哪了"。多 agent 并行(Codex / Claude 4.8 / review agent)
> 时的唯一状态源。
> **不替代** `docs/backlog.md`(债务项)和 `docs/agent-playbook.md`(踩坑案例 / 历史
> timeline)——本文件只管活动任务的分配与状态。

---

## 当前活动项

| 任务 | 负责 | 依赖 | 状态 | PR |
|---|---|---|---|---|
| Lineage parse_errors 明细进 analyze parse_summary(UI 一等公民后端侧) | Claude 子代理(Fable 5 复核) | #78 #79 | PR 待审 | 本 PR |
| Lineage 解析覆盖扩展(CTAS/MERGE/UPDATE/CTE/UNION) | Claude 子代理(Fable 5 复核) | #81 | PR 待审 | 本 PR |

> **2.0.0 GA 已发布**:回归走查 + 安全自审见 `docs/acceptance-2.0.0-ga.md`(10/10,
> 安全项全闭环/落档)。**DM 已 Certified**:真实例验证 8/8 + 修复 3 个 adapter bug,
> 见 `docs/acceptance-dm-certified.md`([#53](https://github.com/allen-answer/dataOpsStudio_v2/pull/53))。
> **1.x 已割接停运(2026-06-12)**:真实数据迁入生产 2.0(演练→割接),1.x 容器已停
> (数据留盘可回滚),2.0 全栈由 API 进程单端口伺服。
>
> **设计基线**:技术方案 **v0.3.3**(2026-06-12,[#60](https://github.com/allen-answer/dataOpsStudio_v2/pull/60))——
> Compare(2.2.0)/ Lineage(2.4.0)按 1.x 使用反馈 8 条 + 业界调研修订;
> 新增 ADR-0019(子图优先)、ADR-0020(血缘边表持久化 + sql_hash 缓存)。
>
> **2.2.0 Compare 能力域已上线生产(2026-06-19)**:跨库规范化哈希内核 + compare_run
> 4 桶 + 映射/主键推断 + 差异画像/采样/AI 归因 + 前端,全部合并,服务器部署到 d7218ef
> 并经 Fable 5 真机走查(真 MySQL+DM,4 桶/单元格分裂/画像逐项验通)。
>
> **★ roadmap 重排(2026-06-19,用户拍板)**:**Scenario Lab 移至 2.6.0**(与 AI Copilot
> 一体——立项调研挖 1.x 源码发现其 DSL 是"结构确定 + AI fill"三层架构,AI fill 是灵魂,
> 且 workload 依赖 Lineage/Workflow;1.x 测绘结论存 `docs/backlog.md`)。**Lineage 提到
> 2.3.0**(v0.3.3 设计稿 + ADR-0019/0020 就绪,纯确定性解析无 AI 依赖)。新排期:
> 2.3 Lineage → 2.4 Workflow → 2.6 Copilot + Scenario。下一能力域 = **2.3.0 Lineage**。
>
> **2.1.0 SQL Workspace 完整版已上线生产(2026-06-14)**:W1 地基 + W2(元数据浏览器
> / format / expand-star / EXPLAIN / 4 格式导出)全部合并,服务器已部署到 07624ef
> 并经 Fable 5 真机走查。
>
> **后续候选(待人拍板,均无 owner/未排期)**:test_connection 终态 job 不阻塞数据源
> 删除(`docs/backlog.md`,2.0.x patch)、Oracle adapter(2.0.x)、外部安全审计
> 触发项(密码进 body 改造)、正式 license 签发(首个商业交付前)、worker DM 加密库
> 部署文档(首个 DM 客户前)、DM 列头 driver_type 美化(`docs/backlog.md`,2.1.x)、
> Compare 导出端点(前端 PR5 留口待后端补,见下)、**2.3.0 Scenario Lab 能力域**
> (设计 v0.3.3:DSL + materialize + verify + run-all)。

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

> **2.2.0 Compare 能力域(2026-06-19 全部合并 + 已上生产)**:
> PR1 跨库规范化哈希内核(Codex,[#70](https://github.com/allen-answer/dataOpsStudio_v2/pull/70)):
> 规范化 SQL + 递归 hashdiff + 逐行 MD5/SUM,MySQL+DM 黄金一致性真机过(5 轮纠错:
> %转义/CAST/精度/位宽/Horner);DM 100K PoC 2.1s。
> PR2 compare_run + 4 桶 + CompareTask 持久化(Codex,[#71](https://github.com/allen-answer/dataOpsStudio_v2/pull/71))。
> PR3 映射/主键自动推断 + 任务建议(Codex,[#72](https://github.com/allen-answer/dataOpsStudio_v2/pull/72);
> 顺带修 DM list_columns 保留字 comment bug)。
> PR4 差异画像 diff_profile + 采样快检 + AI 归因(Codex,[#73](https://github.com/allen-answer/dataOpsStudio_v2/pull/73);
> R-AI 守严:AI 只收脱敏画像 JSON,行值不出境;恒定偏移/时区自动检测真机验通)。
> PR5 前端(**Claude Opus 子代理**,[#74](https://github.com/allen-answer/dataOpsStudio_v2/pull/74)):
> 任务列表/8步推断确认/4桶+单元格分裂/画像面板/采样/AI 入口。
> FK 修复(Codex,[#75](https://github.com/allen-answer/dataOpsStudio_v2/pull/75)):run_compare_task
> 的 jobs/run_index 插入顺序(前端真机走查首次触发的外键违约 500,PR2 fake backend 盲区)。
> 全部 Fable 5 复核 + 真机验证(证据在各 PR 评论)。生产部署 d7218ef。
>
> **2.1.0 Wave 2 — SQL Workspace 工具链(2026-06-14 全部合并 + 已上生产)**:
> W2-PR1 元数据浏览器 + SQL 工具后端(Codex,[#65](https://github.com/allen-answer/dataOpsStudio_v2/pull/65)):
> metadata schemas/tables/columns(MySQL+DM,7 天缓存 + 探测超时 503)/ format /
> expand-star / EXPLAIN(走 job 队列)。
> W2-PR2 4 格式导出后端(Codex,[#66](https://github.com/allen-answer/dataOpsStudio_v2/pull/66)):
> csv/excel/json/sql + 公式注入防御 + 一次性下载 token(原子消费 + 哈希存储 + 属主校验)+
> 每小时限额 + 24h 文件 GC。
> W2-PR3 前端(**Claude Opus 子代理**,[#67](https://github.com/allen-answer/dataOpsStudio_v2/pull/67)):
> 元数据树 + 工具栏 + Result/Plan/Stats 三 tab + 导出 UI。三 PR 均 Fable 5 复核 +
> 真机走查(证据在 PR 评论)。生产部署 07624ef。分工:后端 Codex / 前端 Opus 子代理 /
> Fable 复核(2026-06-13 起)。
>
> **2.1.0 Wave 1 — SQL Workspace 地基(2026-06-13 全部合并 + 已上生产)**:
> W1-PR1 后端(Codex):Console 持久化 / SQL 历史(jobs 推导)/ 模板 CRUD /
> ResultSet 完整版(边拉边看 + 限额 + LRU + TTL 周期 GC)
> ([#62](https://github.com/allen-answer/dataOpsStudio_v2/pull/62))。
> W1-PR2 前端(Codex):多 Console / 历史灌回 / 模板变量插入 / 渐进结果展示
> ([#63](https://github.com/allen-answer/dataOpsStudio_v2/pull/63);走查发现并修复:
> DM 执行封锁过期 → 白名单、终态耗时随挂钟上涨 → 服务端真值 + 契约测试锚定,
> 踩坑案例见 playbook §2)。两 PR 均 Fable 5 真机走查(真生产后端,证据在 PR 评论)。
> 生产部署 9b7f32e,终验:DM 0.4s 冻结、零 console error。
>
> **GA 后生产化 + 设计期(2026-06-12 全部合并)**:
> migrate_from_v1 全局数据源迁入指定项目([#57](https://github.com/allen-answer/dataOpsStudio_v2/pull/57),Codex);
> API 进程伺服前端 SPA dist([#58](https://github.com/allen-answer/dataOpsStudio_v2/pull/58),Claude Opus 4.8);
> 数据源删除策略缺陷立项 backlog([#59](https://github.com/allen-answer/dataOpsStudio_v2/pull/59));
> 设计稿 v0.3.3 — Compare/Lineage 智能化修订 + 调研增补 + ADR-0019/0020
> ([#60](https://github.com/allen-answer/dataOpsStudio_v2/pull/60),Codex 落稿 / Fable 5 复核)。
> 同期非代码事项:1.x 数据迁移演练 + 生产割接 + 1.x 停运;DM 真连通验证经反向隧道
> 跑通;服务器旧备份清理。
>
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
