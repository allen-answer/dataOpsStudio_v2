# ADR-0023: SQL 控制台会话式执行（Session Broker）

- 状态：Proposed（本 PR 为阶段 A 的 A0 验收物；所有者批准并合并后视为 Accepted）
- 日期：2026-08-22
- 取代：**ADR-0021**（见下节“与 ADR-0021 / ADR-0022 的关系”——被取代的是其“不创建跨请求
  query session”的连接边界结论；其分页与数据库限行决策在 job 路径上继续有效并被本 ADR 继承）
- 范围：SQL 控制台（MySQL / DM）的执行通路、取消与超时、会话注册表、`app/broker/` 与
  `app/dbclients/interactive/` 新缝、SQL Workspace 前端绑定方式
- 代码基线：`f137ee23`（#211）。文中 `file:line` 均以该基线核实
- 输入：Session Broker 详细设计（2026-08-22 所有者批准，含评审修订 R1 取消栅栏 / R2 保险带
  逐语句同步 / R3 GC 验收）、DM 会话取消/超时 spike 实证报告、GPT-5.6 dmPython 研究报告、
  数据库用户特性终裁（五条件）

---

## 背景

现状是“每语句一个 job + 用完即弃连接”：API 落 job → worker 单槽串行 claim
（`app/infrastructure/jobbackend/postgres.py`，FOR UPDATE SKIP LOCKED）→ adapter 每次新建连接
执行（`app/dbclients/dm_adapter.py:405-423`）→ 结果写 spool → 前端轮询。由此产生三个结构性
缺陷：

1. **取消不可靠**。取消是纯软取消（DB flag + `fetchmany` 批间检查）。语句阻塞在 `execute`
   阶段时，批间检查根本不会被执行，用户点“取消”后数据库仍在跑——这不是参数问题，是
   “没有第二条控制通路”的结构性根因。
2. **语句超时在 DM 上是空接线**。`app/dbclients/dm_adapter.py:419-422` 把连接超时秒数 ×1000
   塞进 `connection_timeout`（该属性是登录超时，非语句超时）；`:430-437` 给驱动上不存在的
   `callTimeout` 赋值并吞掉异常。实证：`connection_timeout=2` 下 12.7s 长查询未被打断。
3. **无会话连续性**。每语句一条新连接 ⇒ 无 `SET` 变量、无临时表、无跨语句事务，且每语句
   付一次建连 + 凭据 reveal 的握手成本。

ADR-0021 在 2026-08-12 显式否决了长生命周期游标会话，理由是“当前架构没有这些安全边界”，
并列出八项缺失前提：**worker 亲和、租户配额、空闲 TTL、异常接管、会话重置、凭据版本失效、
坏连接淘汰、租户/数据源隔离协议**。该判断在当时成立。此后两项前提发生变化：

- **DM 侧取消能力已实证**：独立控制连接调 `SP_CANCEL_SESSION_OPERATION` 2ms 生效、
  错误码 -6515 精确识别、**取消后连接与未提交事务保留可续用**、`fetchmany` 分批无需重隔离。
  即 DM 与 MySQL（`KILL QUERY` → 1317）可按同构模型处理，不需要“每 console 一个专属子进程”。
- **所有者终裁确定了双模型并存**：既有 job 队列继续服务 compare/批处理/元数据/血缘，console
  走会话式；两条路径**共享深模块，不共享连接**。会话不需要接管 job 路径的资源治理承诺。

因此本 ADR 重新提出会话式执行，并对 ADR-0021 的八项缺失前提逐项给出答案（见专节）。

---

## 决策

### D1：Session Broker 是 **API 进程内组件**，不是独立进程，也不进 Worker

三个候选的判定：

| 候选 | 判定 | 理由 |
|---|---|---|
| 独立 Broker 进程 | 否 | 其唯一原始动因是“DM 每活跃 console 专属子进程需要专职父进程监督”，该动因已被 spike 降级为普通 owner lane 线程。剩余收益只有崩溃隔离，但 launcher 现行监督策略是**任一子进程死亡即全停**（`app/launcher.py:344-352`）——进程隔离在当前部署形态下不真正缩小爆炸半径。代价却是实打实的：第三个 launcher 子进程、IPC/命令总线、独立心跳与接管协议、GUI 包体积与运维面。单用户桌面形态下不成立 |
| Worker 进程内组件 | 否 | Worker 单 job 串行槽是承重设计（`app/worker.py:1618-1623`）。会话线程虽不占该槽，但 compare 等批任务的内存峰值与崩溃会连带杀死所有活会话——批处理恰是本产品最重的负载。且 API↔Worker 之间必须新建命令总线（PG 轮询或 LISTEN/NOTIFY）才能达到交互延迟要求，attach/submit/cancel 全部多一跳 |
| **API 进程内组件（选定）** | ✅ | ① submit/observe/cancel 是进程内函数调用，零 IPC，交互延迟最优，整条命令总线设施及其全部失效模式从设计中删除；② 仓内已有同形先例：workflow cron 调度器就是 API 进程内后台线程（`app/api/app.py:33-44`、`app/config.py:136-142` 明文“无独立调度进程”）；③ API 进程不跑批任务，内存与崩溃特性比 Worker 稳定，会话存活性反而更好；④ DM 官方 DEM 工具即“Web 进程内 SessionPool”形态；⑤ R1 红线不受影响——限制的是 `app/services/` import 驱动，broker 只 import `app/dbclients/interactive/` 的 seam，驱动 import 仍只在 `app/dbclients/` 内 |

**风险承认**：dmPython 若段错误将带倒 API 进程。但现行架构下它段错误同样带倒 Worker 且
launcher 全停——**本决策没有恶化现状**。launcher 改“重启子进程”策略是独立的可选加固，
不在本 ADR 范围。

### D2：拆进程的触发条件与不变量（escape hatch）

Broker 对外表面是**进程无关**的：前端只与 REST 契约交互，会话注册表持久化在 PG。因此
“进程内”是可撤销决定。**出现下列任一条件时，重开进程形态评审**：

| # | 触发条件 | 判据 | 依据 |
|---|---|---|---|
| T1 | 部署形态走向 hosted **多 API 实例** | 出现 >1 个 API 副本、会话亲和不再天然成立（同题先例：ADR-0009 已声明单 API 实例是受支持拓扑） | D1 判定表逐条依赖“单实例 + 单用户桌面形态” |
| T2 | dmPython 段错误成为**实测事实** | 生产或长跑压测中出现 ≥1 次可归因于驱动的 API 进程段错误 / 异常终止 | 本机编译带 `-w -Wno-incompatible-pointer-types`，风险非零 |
| T3 | launcher 监督策略改为**按子进程重启** | `app/launcher.py` 的“任一子进程死亡即全停”被替换 | 该策略正是“进程隔离收益名存实亡”的前提；前提反转则收益复现 |

**明确不构成触发条件**（防蔓延）：会话数量增长本身、“想要崩溃隔离”这一目标本身（在 T3 未
发生前它拿不到真实收益）、单条慢查询占用一条线程。

拆分时**不变**：`/api/sql/sessions*` 与 `/api/sql/statements*` REST 契约、`console_sessions` /
`console_statements` / `console_statement_events` 数据模型、epoch fencing 语义、前端。
拆分时**须补**：launcher 第三子进程与其监督/心跳、一条命令总线（attach/submit/cancel 的跨
进程投递 + 背压）、broker 接管协议（`broker_boot_id` 清扫已为此预留）。

### D3：并发模型与取消正确性

dmPython / PyMySQL 均为 `threadsafety=1`，连接不可跨线程共享。所有权规则：

1. **一个会话 = 一条 lane 线程 = 独占一条 InteractiveConnection**。lane 从有界 mailbox
   （默认 20）串行取命令，驱动调用只发生在 lane 线程上。同 console 并发提交 = 排队，不并发执行。
2. **每 datasource 一条 control lane 线程 + 一条常驻控制连接**，同 datasource 的会话共享；
   控制命令（cancel/destroy）在其上串行。懒建、末个会话关闭后延迟回收。
3. **一条 Timer 线程**统一驱动语句 deadline 与空闲回收，只派发请求，自己不碰任何驱动连接。
4. **串行性是取消正确性的根基**：lane 严格串行 ⇒ 该会话服务端同时至多一个在执行的操作 ⇒
   控制连接发出的 `SP_CANCEL` / `KILL QUERY` 只可能命中“当前语句”或空转。
   **取消栅栏**：这两个原语是会话级的（杀“当前操作”，不认语句号），因此规定——**会话存在
   在途取消（已派发给 control lane、尚未收到确认或空转回报）期间，lane 不得启动下一条语句**，
   栅栏解除后才继续取 mailbox。该不变量必须落为单测。
5. 凭据所有权不变：broker 只持 `SecretRef`，明文只在 `dbclients` 的 connect 内部短暂存在，
   每次建连都 reveal + 审计。会话复用连接 = 减少 reveal 次数，**不引入任何明文缓存**。

### D4：取消与超时

取消阶梯（每一级都落 `console_statement_events` 留痕）：

1. 软取消 flag，lane 在 `fetchmany` 批间检查——覆盖“已在流式取数”阶段的零成本路径；
2. **硬取消**：control lane 发 `SP_CANCEL_SESSION_OPERATION(:sid)` / `KILL QUERY :id`——覆盖
   `execute` 阻塞阶段，即今天软取消到不了的地方；
3. 宽限期（默认 5s）未见服务器确认 → **destroy**（`SP_CLOSE_SESSION` / `KILL CONNECTION`）
   → `session_lost`；
4. destroy 也失败 → 放弃连接、`session_lost`。**任何一步失败都如实上报，绝不显示“已取消”
   而实际仍在跑**。

`server_confirmed` 与 `connection_aborted` 的区分**只按数值错误码，永不按消息文本**——错误
消息语言是生产环境配置项。DM `SP_CANCEL` 的生产账号权限未证，故在 datasource 首条控制连接
建立时对**自己的 sid** 试调一次（自会话空转，无害）：权限错 → 该 datasource 标记
`server_cancel=degraded`，取消降级为“软取消 + destroy 兜底”，attach 响应如实携带，前端明示
“该数据源取消将断开会话”。

**语句时限 = broker 客户端计时到点走取消阶梯**，两方言统一（DM 无服务端语句超时属性，已实证）。
MySQL 额外设 `SET SESSION max_execution_time` 作保险带（默认语句超时 +30s），且**语句生效超时
与会话当前值不同时（含 0=不限）于 execute 前重设**，避免“不限时”语句被默认带误杀。会话连接
上不再设 `read_timeout`（它会误杀长查询；job 路径设它是一次性连接才成立的做法）。

### D5：epoch fencing 与幂等回执

`sql_consoles.session_epoch BIGINT`，**每次 attach 原子 +1**，随控制台持久、跨 broker 重启单调。
**变更类请求（submit/cancel/close/txn）必须携带 epoch 且等于当前值，否则 409
`stale_session_epoch`；观察类请求不设防，但响应总是携带 `current_epoch`**，被接管的旧 tab 据此
渲染“已被另一窗口接管”。双 tab 抢占、断线重连、执行中 attach、并发 attach 相撞、broker 重启、
close/submit 竞速、cancel 与语句完成竞速、陈旧 cancel 共八个竞态（M1–M8）逐条判定，落为单测矩阵。

`UNIQUE(session_id, client_request_id)` 为幂等回执键：重发同键 submit **只返回原语句行**
（`deduplicated=true`），永不重执行。“永不自动重试”约束的对象是**语句执行**，回执机制是它的
配套——它让前端可以安全重试**提交请求**而不产生第二次执行。

### D6：状态诚实性

- `session_lost` 是**诚实终态**：连接不可恢复即宣告丢失，绝不伪装可续用；前端“重新连接”=
  新 attach、新会话、新 epoch，并显式告知会话状态（临时表 / 事务）已失。
- `outcome_unknown` **仅 `is_write=true` 的语句可进入**（只读语句的同类故障一律归
  `failed(connection_aborted)`——SELECT 无副作用，不制造人工核实负担）。**退出只有人工**：
  UI 三选一 + 备注，写 `resolved_by / resolved_at / resolution`，**原始状态不可改写**（保留
  证据链）。无任何自动重试或自动判定路径。
- `cancelled` / `timeout` 的语句**保留已落 spool 的部分结果**（truncated 标注，“已取消，
  保留前 N 行”）。这与 job 路径“取消即删 spool”（`app/worker.py:2028-2029`）不同，属自觉的
  行为偏离，见“后果”。

### D7：范围线与回退开关

只读纵切（阶段 A）含：会话绑定与注册表持久化、启动清扫、SELECT 路由、取消阶梯与探测降级、
统一超时、空闲回收、前端体验闭环与 progress / result / 导出 / 历史 / 结果集淘汰全平价。
`validate_readonly_sql`（`app/dbclients/sql_guard.py:33-56`）**原样不动**。

**明确不含**：写能力与脚本切分、autocommit 与手动事务、临时表 / SET 连续性（被 guard 挡住，
写阶段随 guard 放开自然解锁——诚实声明：**只读片的“连续性”用户可感知面是取消可靠 + 免握手
延迟，不是临时表**）、schema 浏览器、compare / 元数据 / 血缘路径、Oracle/DB2/PG 会话、
WS/SSE、跨会话连接池、审计异步化、launcher 监督策略变更。

回退开关：全局配置 `console_session_enabled`（默认 on），off 时前端整体回落 job 路径。这是
上线保险丝，不是长期分支。

---

## ADR-0021 八项缺失前提的逐项回答

| # | ADR-0021 列出的缺失前提 | 本设计的答案 | 落点 |
|---|---|---|---|
| 1 | **worker 亲和** | 前提被消解而非满足：会话不进 worker（D1），单 API 进程内即天然亲和，无跨进程路由问题。拆进程后由 REST 契约 + PG 注册表 + 命令总线承担 | D1 / D2 |
| 2 | **租户配额** | 会话数护栏：单用户活跃会话 ≤ 8、单 datasource ≤ 4、进程全局 ≤ 16，超限 attach 返回 409 `session_limit_reached`；`sql_consoles` 本身已有 20/用户上限（`app/api/routes/core.py:2067-2072`）。mailbox 有界 20 防单会话积压 | D3 / 开放参数 |
| 3 | **空闲 TTL** | Timer 线程统一驱动，默认 1800s（写阶段有未提交事务时 3600s，且 observe 载荷暴露 `idle_deadline` 供前端 T-5min 警示）；回收动作 = rollback + close，`close_reason=idle_timeout`，**绝不静默 commit** | D3-3 / D6 |
| 4 | **异常接管** | epoch fencing 八竞态矩阵 M1–M8（D5）+ 启动清扫：`broker_boot_id ≠ 本次 boot` 的 active 行 → `session_lost(close_reason=broker_restart)`，执行中语句按读 → failed / 写 → outcome_unknown 收敛 | D5 / D6 |
| 5 | **会话重置（session reset）** | 前提被消解：**不做 reset，做会话所有权 + 诚实丢失**。一条连接终身归单一 console / 单一属主，从不跨用户、跨 console、跨语句上下文复用，因此不需要“统一、可验证的 session reset 协议”；连接一旦不可续用即 `session_lost`，不回收进任何池 | D3-1 / D6 |
| 6 | **凭据版本失效** | 无明文缓存可失效（只持 `SecretRef`）。已建连接不受轮换影响（凭据只在登录期使用）；控制连接与新会话建连时经 secretstore 取新值，控制连接“使用前 ping、失败即以当前凭据重建”天然吸收轮换 | D3-5 / D4 |
| 7 | **坏连接淘汰** | 无池即无“淘汰”语义需求：owner 连接坏 → 错误分类 `connection_dead` → `session_lost`（不复用、不自动重连、不自动重放）；control 连接坏 → ping 检出即重建，重建窗口内的 cancel 直接升阶梯或如实报 `cancel_failed`（语句仍在跑，UI 给 destroy 选项）。**控制连接故障永不影响 owner lane 正常完成** | D4 |
| 8 | **租户 / 数据源隔离协议** | 隔离由“不共享”实现而非由协议实现：lane 连接归单 console / 单属主；control lane 按 datasource 划分且只发**会话级**控制原语，作用对象仅限本进程自采的 `session_marker`（DM `SESSID()` / MySQL `CONNECTION_ID()`）；连接池化列入显式不做 | D3 / D7 |

---

## 与 ADR-0021 / ADR-0022 的关系

- **被本 ADR 取代**：ADR-0021“连接与资源边界”一节的结论——“本方案不创建跨请求 query
  session”“在这些条件齐备前复用连接会扩大凭据更新和 session 污染风险”。上表逐项说明这些
  条件如何被满足或被消解；且“复用连接”在本设计中的含义是**单一会话内的连接连续性**，不是
  跨请求 / 跨用户的连接池——后者仍被显式否决。
- **继续有效并被继承**：ADR-0021 的其余决策（`page_size` 与 `max_result_rows` 分离、AST 窗口
  限行、首批即写 spool、无状态续页 job、顶层 `ORDER BY` 前置条件、不自动 `COUNT(*)`、
  分阶段耗时、预览截断）在 **job 路径上原样保留**，且 console 会话路径复用同一 ResultStore
  与结果契约。双模型并存是所有者终裁，不是过渡态。
- **ADR-0022 全部继承**：语句 progress 与 `JobProgressResponse` **字段镜像**（含
  `result_version / loaded_rows / columns_ready / first_batch_ready / terminal / retry_after_ms /
  truncated / has_more`），服务端 `retry_after_ms` 调速、自适应退避、页面可见性降频、跨 tab
  lease、BroadcastChannel、代际守卫按 statement 维度复用；载荷脱敏纪律（不含 SQL 原文、绑定
  参数、主机地址）沿用；审计豁免清单追加 statement progress / result GET。限流沿用既有分组桶
  （`app/api/services.py:46-53`）：attach/submit/cancel/close → `sql_control`（30/min），
  observe/progress/result → `job_read`（300/min）。**本 ADR 同样不引入 WS/SSE**——ADR-0022 已
  论证轮询契约可被未来推送层复用而不改 UI 状态模型。
- ADR-0022“未引入数据库 cursor session / worker affinity / 连接池”的表述，在 console 路径上由
  本 ADR 更新；job 路径的该表述不变。

---

## 后果

**收益**

- 取消从“批间软检查”变为“控制连接硬取消”，覆盖 `execute` 阻塞阶段——取消不可靠的结构性
  根因被消除（DM 实证 2ms 生效且连接续用）。
- 语句超时在 DM 上第一次真实生效（客户端计时 + 取消阶梯），登录超时属性 / 单位错误在**新缝
  内**修正（旧 adapter 不动，job 路径行为零变化）。
- 每语句一次建连 + 凭据 reveal 的握手成本降为每会话一次；reveal 次数下降本身也收窄了明文
  暴露面。
- Worker 崩溃对会话零影响；批处理与交互执行的资源竞争被拆开。

**代价与已知行为变化**（需所有者知情）

1. 同 console 多语句由**并行改串行**（现前端 `frontend/src/views/SqlWorkspaceView.vue:842-864`
   为 `Promise.all` 并行提交）。这是 DataGrip 语义，也是会话连续性与取消正确性的前提。
2. 取消的 console 语句**保留部分结果**，与 job 路径“取消即删 spool”不一致；两条路径在这一点
   上行为不同，UI 需如实标注。
3. API 进程新增三类常驻线程（lane / control / timer）与用户库长连接：API 进程的内存与句柄画像
   变化，且 dmPython 段错误会带倒 API 进程（现状同型，见 D1 风险承认）。
4. 新增三张表与 `sql_consoles.session_epoch` 列；`console_statements.sql_text` 存完整原文
   （PG 存储非日志——R5 约束的是 structlog，日志仍仅 `sql_hash` + 长度）。
5. 双路径长期共存 ⇒ 契约漂移风险。以 **progress schema 镜像断言**作为防漂移的可执行护栏。

**失效模式的兜底**（均有对应设计与验收用例）：broker 重启 → 启动清扫；连接死亡 →
`session_lost` 且不自动重连；PG 短暂不可用 → 内存注册表为运行时权威，在跑语句不中断、
attach/submit 诚实 5xx；spool 写失败 → `failed(resultstore_error)` 且会话续用；控制连接不可用
→ 升阶梯或如实 `cancel_failed`；lane 积压 → mailbox 满则 409。**worker 的 spool GC 不得回收
活跃会话语句的结果集**（与 job 结果共用 `result_sets` catalog），须有专门用例。

---

## 不采用的方案

| 方案 | 不采用理由 |
|---|---|
| 独立 Broker 进程 / Worker 内组件 | 见 D1 判定表。触发条件见 D2——不是永久否决 |
| DM 每活跃 console 专属子进程 | 其动因（取消能力未验证）已被 spike 消解 |
| 跨会话连接池 | 会话所有权 ≠ 池化。池化会重新引入 ADR-0021 第 5–8 项前提（session reset / 凭据失效 / 坏连接淘汰 / 隔离协议），本设计正是靠“不共享”回避它们 |
| WS/SSE 推送层 | ADR-0022 已论证轮询契约足够且可被未来推送层复用；仓内零 WS/SSE 存量 |
| 把 `connection_timeout` 当服务端语句硬上限 | 实证证伪（=2 时 12.7s 查询未被打断）。设置一个无效属性只制造虚假安全感 |
| 升级 dmPython ≥2.5.33 / 启用 `lazy_rowcount` | 2.5.32 实证 `execute` 不缓冲、RSS 平、`fetchmany` 亚毫秒；与生产同版实证的价值高于新特性。若大结果集 rowcount 预取成为实测瓶颈再评估 |
| 扩展现有 `DatabaseAdapter` 承载会话 | 所有者终裁：会话式是**新 Protocol 新缝**（`app/dbclients/interactive/`，契约 §3.8），既有 adapter 与 `AdapterCapabilities` 不动，避免 job 路径被交互语义污染 |

---

## 开放参数（默认值，所有者可调）

会话上限 8（每用户）/ 4（每 datasource）/ 16（每进程）、空闲回收 1800s（写阶段事务中 3600s）、
语句超时 600s（0 = 不限，DataGrip 语义）、取消宽限 5s、mailbox 上限 20、
`console_session_enabled=true`。

**留给实现期的内网复核**（均已有降级路径兜底，不阻塞开工）：生产账号
`SP_CANCEL_SESSION_OPERATION` 权限（探测 + degraded 已备）、生产错误消息语言配置（分类只认
数值码已免疫，仅影响展示文案）、`SP_CLOSE_SESSION` 回滚时延（destroy 兜底路径的宽限期校准）。
