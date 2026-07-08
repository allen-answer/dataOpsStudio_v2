# DataOpsStudio 2.0 进度看板(多 agent 同步)

> **用途**:同步"谁在做什么、做到哪了"。多 agent 并行(Codex / Claude 4.8 / review agent)
> 时的唯一状态源。
> **不替代** `docs/backlog.md`(债务项)和 `docs/agent-playbook.md`(踩坑案例 / 历史
> timeline)——本文件只管活动任务的分配与状态。

---

## 当前活动项

| 任务 | 负责 | 依赖 | 状态 | PR |
|---|---|---|---|---|
| 真机深走查(Lineage/Workflow/Compare/AI 新面,带登录) | 待人排期(需 admin 凭据) | 生产已在 5b13659 | 未开工 | — |

> **★ 生产部署 5b13659(2026-07-08 晚,Fable 5)**:ac62585 → 5b13659(#153–#169 全量)。
> 迁移 0022/0023/0024 干净应用;四进程绿 + healthz 200 + ai/sql-generate 401(非 404)+
> 新 dist(index-Ceqxk999.js)。
>
> **★ Win10 全量离线部署包(2026-07-08,[#171](https://github.com/allen-answer/dataOpsStudio_v2/pull/171))**:
> 打包脚本 + 文档合入;成品 `dataops-studio-2.0.1-win10-x64-offline.zip`(192.5MB,
> 内嵌 CPython 3.12 + PG 16.14 精简二进制 + 全驱动 wheels(DM 专有客户端除外)+ 前端 dist
> + start/stop 脚本),本机冷启动冒烟全过(离线装依赖→bootstrap→24 步迁移→登录 200)。
> 产物在构建机 `dist-bundle/`(git-ignored),sha256 见 PR body。

> **★ 三波并行开发全部合并(2026-07-08,Opus 4.8 子代理开发 / Fable 5 复核,#153–#169 共 17 PR)**:
> **wave-1**:Compare 导出解码业务列([#153](https://github.com/allen-answer/dataOpsStudio_v2/pull/153))、
> Oracle adapter Preview([#154](https://github.com/allen-answer/dataOpsStudio_v2/pull/154),thin 免客户端,PR 附 Certified 前置清单)、
> **AI Copilot C1 NL→SQL**([#155](https://github.com/allen-answer/dataOpsStudio_v2/pull/155),L2)、
> Workflow 管理前端 v1 + run 历史端点([#156](https://github.com/allen-answer/dataOpsStudio_v2/pull/156))、
> UX-2 批次懒展开/差异行 SQL/主键预检([#157](https://github.com/allen-answer/dataOpsStudio_v2/pull/157))、
> Workflow cron tick PR-4b([#158](https://github.com/allen-answer/dataOpsStudio_v2/pull/158),迁移 0022)。
> **wave-2**:PG adapter Certified CI 真 PG 验证([#160](https://github.com/allen-answer/dataOpsStudio_v2/pull/160),
> statement_timeout/jsonb/注入面/cancel_safe 七测全过,`docs/acceptance-pg-certified.md`)、
> L-1 方言自动识别([#161](https://github.com/allen-answer/dataOpsStudio_v2/pull/161))、
> C-11 preflight + C-12 run 仪表盘([#162](https://github.com/allen-answer/dataOpsStudio_v2/pull/162))、
> C-10 sensor 触发([#163](https://github.com/allen-answer/dataOpsStudio_v2/pull/163),迁移 0023)、
> C-8 trace_compare 逐跳对比([#164](https://github.com/allen-answer/dataOpsStudio_v2/pull/164))、
> L-7 血缘报告 AI enrichment([#165](https://github.com/allen-answer/dataOpsStudio_v2/pull/165),迁移 0024,append-only)。
> **wave-3(Copilot 提前交付,无历史优雅降级)**:C4 慢 SQL 根因诊断([#168](https://github.com/allen-answer/dataOpsStudio_v2/pull/168),L3)、
> C3 加权影响([#167](https://github.com/allen-answer/dataOpsStudio_v2/pull/167),L2)、
> C2 映射学习([#169](https://github.com/allen-answer/dataOpsStudio_v2/pull/169),L4 双重 opt-in)。
> **修复件**:workflow compare_run 子 job 登记接线([#166](https://github.com/allen-answer/dataOpsStudio_v2/pull/166),
> 解锁 C-7 模板与 C-8 生成的 workflow 真正可跑)。
> 至此 **AI Copilot 五场景除 C5(依赖 Scenario Lab,2.6.0)外全部落地**;差距矩阵除
> L-10(OpenLineage,等外部 collector 需求)/ L-12(资产域未立项)/ 徽章类外全部收口。
> 过程要点:#158/#165 迁移 0022 撞号(重编号 0024 解决);squash 合并触发多轮 rebase
> 由子代理逐一处理;全部 PR 合并前 CI 10/10 绿。

> **★ 生产部署 ac62585(2026-07-08,Fable 5 按 upgrade-in-place.md 执行)**:
> 20f18f5 → ac62585(#141–#152 全量:C-7 ${var} 插值三连、C-9 通知全量含 SMTP、
> compare 导出 token/dedup/历史入口、PG cancel_safe、workflow run 修复包、#150 设计稿、
> #152 PUT preserve-on-omit)。迁移 0021 干净应用;四进程绿 + healthz 200 +
> notify-targets 401(非 404)+ 新 dist(index-BgJ9UWxv.js)。
> 踩坑:fresh 实例所有 launcher 子命令必须带 `DATAOPS_PG_PORT=16432`,
> 否则打到同机老实例 15432 的 PG(报错伪装成密码错误)。
>
> **2.5.0 两大档拍板已被事实超越**:Lineage 深度报告(L-2/L-3/L-4/L-5)与
> Compare 文件源(C-1)均已全部合并上线,该决策项闭环。

> **★ 差距收口 + UX 优化第一波(2026-07-02/03 全部合并)**:
> 1.x vs 2.0 差距测绘立项底稿([#100](https://github.com/allen-answer/dataOpsStudio_v2/pull/100),
> 三路考古,含两处伪差距更正);收口件七连 —— 血缘 postgres 方言
> ([#101](https://github.com/allen-answer/dataOpsStudio_v2/pull/101))、任务复制
> ([#102](https://github.com/allen-answer/dataOpsStudio_v2/pull/102))、推断边确认/拒绝 UI
> ([#103](https://github.com/allen-answer/dataOpsStudio_v2/pull/103))、字段多跳追溯 API
> ([#104](https://github.com/allen-answer/dataOpsStudio_v2/pull/104))、历史列表+数据预览后端
> ([#105](https://github.com/allen-answer/dataOpsStudio_v2/pull/105),顺带抽 sql_build 共享模块)、
> 血缘 xlsx 导出([#106](https://github.com/allen-answer/dataOpsStudio_v2/pull/106))、
> Compare 前端收口 SQL 引用/历史/预览/列编辑器
> ([#109](https://github.com/allen-answer/dataOpsStudio_v2/pull/109),纯 SQL 任务闭环打通)。
> 体验优化:对标调研方案落档([#107](https://github.com/allen-answer/dataOpsStudio_v2/pull/107),
> Datafold/SQLMesh/OpenMetadata/DataHub 等 11 家)+ UX-1 血缘边详情抽屉/图内定位
> ([#108](https://github.com/allen-answer/dataOpsStudio_v2/pull/108),迁移 0018 存 SQL 原文)。
> 全部 Claude 侧完成(子代理开发 + Fable 5 复核/代修),Codex 未参与本波。
>
> **★ 生产部署 433f087(2026-07-02,Fable 5 按 upgrade-in-place.md 执行)**:
> d7218ef → 433f087(#78–#98 全量:2.3.0 Lineage、2.4.0 Workflow PR0–5、DB2 PR-A/B、
> PostgreSQL adapter、Compare 导出、Worker 心跳线程)。迁移 0015/0016/0017 干净应用;
> **`DATAOPS_WORKER_HEARTBEAT_TIMEOUT_SECONDS=90`** 进 launch 行(#97 心跳线程合并后
> ADR-0018 的 600s trade-off 解除,launch 行已记入 `~/dataops-launcher.log` 同款位置)。
> 冒烟:四进程绿 / SPA 登录页真渲染 console 零 error / auth 链路 401 结构化 /
> Workflow+Lineage 新路由 401 非 404。**带登录深走查待人**(admin 凭据仅人持有)。
>
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
> **2.3.0 Scenario Lab 能力域**
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

> **2.3.0 Lineage 能力域主体(2026-07-02 全部合并;真机走查 + 生产部署待排期)**:
> 后端地基(此前已合并):sqlglot 解析核心(Codex,[#78](https://github.com/allen-answer/dataOpsStudio_v2/pull/78))、
> 边表持久化 + 子图/影响端点(ADR-0019/0020,[#79](https://github.com/allen-answer/dataOpsStudio_v2/pull/79))。
> 本轮四连(**Claude 子代理开发 / Fable 5 逐文件复核 + 独立复跑验证**,后端未用 Codex):
> parse_errors 明细进 parse_summary(前 50 条截断,[#81](https://github.com/allen-answer/dataOpsStudio_v2/pull/81));
> 解析覆盖扩展 CTAS/MERGE/多表&相关子查询 UPDATE/CTE 穿透/UNION 分支合并
> (parser_version bump v2 作废 v1 负缓存,[#82](https://github.com/allen-answer/dataOpsStudio_v2/pull/82));
> AI 兜底解析(L2 默认关,字面量遮蔽出站,事务提交后执行不拖垮主路径)+ inferred 边
> confirmed/rejected 状态机([#83](https://github.com/allen-answer/dataOpsStudio_v2/pull/83),
> **5 个设计裁决项见 PR 描述**:触发形态/confidence 缺省/reason 落库/unconfirmed 边
> impact 权重/切分函数收敛);前端子图 SVG 分层 + 影响分析 + SQL 解析三 tab
> ([#80](https://github.com/allen-answer/dataOpsStudio_v2/pull/80),**真机走查未做**——
> 用户拍板先跳过,收口时补)。踩坑:CI mypy 范围是 `mypy app tests`,比本地
> check-redlines 宽,#82/#83 首轮 CI 各红一次(案例已入 `docs/agent-playbook.md §2`)。
>
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
