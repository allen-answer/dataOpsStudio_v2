# Agent Playbook — DataOps Studio 2.0

> 历史 changelog + 踩坑案例 + 设计背景。**按需查,不是 agent 必读**。
> 当前的硬规则在 `AGENTS.md` / `CLAUDE.md`(agent 自动加载) + `AGENT_CONTRACT_v2.md`(开工前读)。

---

## 1. 任务完成时间线(2.0.0 骨架)

| Phase | 任务 | 主力 | 合并 commit | 主要交付 |
|---|---|---|---|---|
| Step 1 | 立围栏 | Claude Code | `499899a`(tag: skeleton-frozen)| 全部 Protocol + 域模型 + R1–R9 CI |
| Step 2 | T1 PG JobBackend | Codex | `78f3bde` | PG queue + reaper + worker;R10 ast-grep |
| Step 2 | T2 MySQL adapter | Codex | `c7bd53f` | MySQL adapter + LocalFsResultStore |
| Step 2 | T3 SecretStore | Codex | (与 Step 1 一起) | Bootstrap + Application 两层 + R5 redaction |
| Step 2 | T4 API + middleware | Codex | `6048960` | API + 中间件 + §5 e2e |
| Step 2 | T4 后补 列名通道 | Codex | `66715f7` | column_sink 全链路 |
| Step 2 | T6 launcher | Codex | `10cb832` | portable cold-start + quickstart |
| Infra | CI cold-start job | Claude Code | `b490c9c` | T6 hard evidence 守护 |
| Step 2 | GA 债清理 | Codex | `1e0a76b` | docs gating + Bootstrap Protocol + DbType 异常拆分 |
| Step 2 | T7 列表端点 | Codex | `fb9fd69` | `GET /api/datasources` / `GET /api/jobs` |
| Step 2 | T7 phase 1 | Claude Code | `4789b05` | design system 预览页 + Frontend Build CI |
| Step 2 | T7 phase 2α | Claude Code | (PR 进行中)| 路由 + auth + login + projects |

---

## 2. 踩坑案例

每个案例结构统一:**症状 / 根因 / 修法 / 教训**。

### 列名通道"看似 OK 实际丢"(T4 后补,commit `66715f7`)

**症状**
- T4 §5 e2e 跑通 r=2,但 `/result` 响应里 `columns` 数组为空,只有 positional values
- 前端拿到 `[[1, "alice"], [2, "bob"]]`,不知道哪列是 id 哪列是 name

**根因**
- `MySQLAdapter.execute_select` 流式路径只 yield `Row(values=...)`,**没用 `cursor.description`**
- spool manifest 无 `columns` 字段;worker 不写 `result_sets` 表
- Schema 给了通道,实现没用上(三模块全漏)

**修法**
- `column_sink` 回调:adapter 把 `cursor.description` 转 `list[Column]` 给 worker
- worker 写 spool manifest **+** 写 `result_sets` 表 —— 两路同源
- API `/result` 从 manifest 读 columns

**教训**
- e2e 跑通不代表协议完整。**验收必须涵盖前端能看到的字段**,不只 status code
- 三模块协同的功能要有跨层 hard evidence test(本次:e2e 升级 `columns[0].name == "r"` + `catalog JSONB == /result 响应`)

---

### Fixture UUID 拼前缀超 VARCHAR(36)(T1 PR #3 → 升 R10 规则)

**症状**
- T1 PR PG integration test 4 个全红

**根因**
- 测试 fixture `f"user-{uuid4().hex}"` = 37 字符,超 VARCHAR(36)

**修法**
- Codex 改用 `str(uuid4())` = 36 字符
- 用户**升级为 codebase-wide R10 规则**:`tools/lint/rules/r10_no_uuid_prefix_concat.yml`,
  ast-grep 全仓扫 `f"...{uuid4()...}"` 模式

**教训**
- Debug 修代码不够,**升规则防同类 bug 再现**
- 加红线时,把"是哪次事故触发的"写进规则注释(R10 有写)

---

### Bootstrap 文件 0600 vs 0400(T6 cold-start verification)

**症状**
- Claude Code 独立 cold-start verification 时,`launcher bootstrap init` 后启动报
  `BootstrapSecretPermissionError`

**根因**
- 验证人 `chmod 600` 设权限(Mac 肌肉记忆)
- 但 launcher 早期要求 0400
- quickstart.md 没说"必须 0400"

**修法**
- Codex 后期把要求改成 0600 让 Mac 肌肉记忆兼容
- quickstart.md 明示

**教训**
- 文档化"为什么 0600 不是 0400",**不让验证人靠错误信息反推**

---

### 前端白屏:Pinia 未就绪时 store 被调用(T7 phase 2α)

**症状**
- Vite dev 起来 OK,build 通过,curl `/login` 返回 200
- 浏览器打开**白屏**,看不到登录表单

**根因**
- `router/index.ts` 顶层调 `installAuthGuard(router)`,模块加载时立刻执行
- `installAuthGuard` 函数体里第一行 `const auth = useAuthStore()`
- `main.ts` 的 import 顺序是 `import { router } from './router'` **先于**
  `app.use(createPinia())`
- 结果:`useAuthStore()` 在 Pinia 未注册时被调用 → `getActivePinia()` 抛异常 →
  `main.ts` module evaluation 崩 → app 永远没 mount → 白屏

**修法**
- `installAuthGuard(router)` 调用从 router 模块顶层挪到 `main.ts`,
  在 `app.use(createPinia())` + `app.use(router)` 之后调用

**教训**
- **build 通过 ≠ 跑起来通过**(build 不执行 router 顶层副作用)
- **curl 200 ≠ 前端真渲染**(只证明 SPA index.html 在,JS 可能挂在 module 阶段)
- ES module 顶层副作用是定时炸弹;**框架插件初始化顺序敏感时,模块只导出零件,
  装配顺序由应用入口 `main.ts` 控制**
- → 已落 `AGENTS.md` / `CLAUDE.md §4` 前端"跑起来"最低标准

---

### Vite plugin pipeline 失活 / .vite cache 漂移(T7 phase 2α)

**症状**
- 长时间运行的 Vite dev server 突然报 `Failed to parse ... Install @vitejs/plugin-vue`
- 但 `@vitejs/plugin-vue` 明明装着,vite.config.ts 也正确

**根因**
- Vite dev 进程从前一天跑到次日,期间 config / tsconfig / SFC 改了几十次
- `node_modules/.vite/` cache 状态在多次 restart 之间漂移
- Plugin 实例注册了但没接管 transform pipeline

**修法**
```bash
kill <vite_pid>
rm -rf node_modules/.vite
npm run dev
```

**教训**
- 长跑 Vite + 反复改 config,**插件诡异失效**第一反应是杀重启 + 清 cache,
  不要在错的状态上 debug 代码
- HMR 不能 100% 替代 cold restart

---

### `gh run watch --exit-status` 假阴性(T7 phase 1)

**症状**
- CI 实际 8/8 全绿,但 `gh run watch --exit-status` 退出 1,误以为 CI 红

**根因**
- GitHub Actions 的 Node.js 20 deprecation warning + cache 服务 400 错误
  被 `--exit-status` 模式当 fail

**修法**
- 不用 `--exit-status`
- 改用 `gh run watch` 然后 `gh run view --json conclusion --jq .conclusion` 直接读结论

**教训**
- CI watcher 的 exit code 不等于 run conclusion。报告 CI 状态前**直接查 conclusion 字段**

---

### SQL 耗时显示三连修:mock 撒谎,契约测试才是锚(2.1.0 W1,PR #63)

**症状**
- 完成态的耗时数字随挂钟持续上涨(16.5s → 40.5s →∞);且初值远大于真实执行时间
  (服务端 job created==finished 同秒,UI 却显示 16.5s)

**根因(三层,逐层揭开)**
1. 前端 `elapsedSeconds = nowMs - startedAt`,`nowMs` 是 setInterval 挂钟,终态无人冻结
2. 一修(本地 finishedAt 冻结)仍依赖本地时钟语义,起点漂移照样虚高
3. 二修(改用服务端 created_at/finished_at)前端 e2e 全绿但真机无效——
   **后端 `GET /jobs/{id}` 根本不返回这两个字段,e2e 的 mock 里却有**。
   mock 与真实 API 契约脱节,测试自证了一个不存在的接口

**修法**
- 后端 JobResponse 补 created_at/finished_at(纯透传)
- **契约测试锚定字段与 ISO 格式**(tests/contract/test_api.py),防 mock 再次漂移
- 终态耗时 = 服务端差值(顺带免疫本地与服务器时钟偏差);运行中才用本地挂钟

**教训**
- 前端 mock 响应里的每个字段,必须有一条**后端契约测试**背书,否则 mock 会撒谎
- 凡是"完成后界面还在动"类 bug,优先查全局 interval 的消费方有没有终态出口
- 真机走查(宪法 §4)不是仪式:本案 e2e 4/4 全绿 + CI 全绿,bug 只在真后端暴露

---

### 凭过期记忆做生产升级:15 分钟停机(2026-06-12)

**症状**
- 例行升级触发生产停机约 15 分钟:停服后 `uv: command not found`、
  alembic 连不上 PG、重启的 tmux 会话秒死

**根因**
- 操作前没读 `docs/deployment/upgrade-in-place.md`,凭会话记忆写脚本:
  会话名记错、漏 `--root` / `--pg-bin-dir`、不知道非交互 SSH shell 没有 uv 的 PATH、
  不知道 portable 形态 PG 是 tmux 会话子进程(kill session 连 PG 一起带走)

**修法**
- 全部坑写进 upgrade-in-place.md §6(Operational pitfalls),含完整生产启动行模板
- 升级脚本一律 `bash -ls` / 顶部 export PATH,健康检查必须出现在脚本末尾

**教训**
- **runbook 存在就必须先读全文再动手**,"我上次就是这么部署的"不算依据
- 任何改运行状态的脚本,第一步先核实"现在到底是什么在跑"(tmux ls + ss -tlnp),
  而不是假设拓扑与记忆一致

---

### a11y 快照走查漏掉 CSS 竖排;chrome-btn-ghost 是图标专用类(2.1.0 W2,PR #67→#69)

**症状**
- W2-PR3 合并并上生产后,用户截图发现结果区三个 tab(结果/执行计划/统计)的中文标签
  被挤成竖排单字(每字一行)。Fable 真机走查时没发现。

**根因(两层)**
1. 视觉 bug 本身:`.chrome-btn-ghost` 是**固定 1.75rem 方块的纯图标按钮**
   (`width/height:1.75rem` + `justify-content:center`,无 `white-space:nowrap`);
   W2-PR3 把它误用在"图标 + 中文文字"的 tab 上,28px 方块把多字中文压成竖排。
   `px-3` 改不动它——`[data-variant] .chrome-btn-ghost` 特异性高过 Tailwind 工具类。
   (此前 Wave 4「登录第二步按钮塌缩」是同一个类的同类误用,属复发。)
2. 走查方法缺陷:复核用的是**无障碍快照(a11y snapshot)**,它读按钮的可访问名
   ("结果"/"统计"),**不反映 CSS 布局**,所以竖排 bug 在快照里看不出来。

**修法**
- 新增语义化 `.chrome-tab`(auto 宽 + `white-space:nowrap` + h-2rem)替代误用;
  tab 组加 `shrink-0`、右侧状态/导出组加 `min-w-0`(让占位长文本截断而非挤压 tab)。
- 全仓 grep `chrome-btn-ghost` 自查,揪出并修了另一处同类误用(AdminAiConfig「清除 Key」)。

**教训**
- **前端走查必须看真截图**(browser_take_screenshot),不能只靠 a11y 快照——
  快照能验"元素在/可点/可访问名对",但验不了"长得对"。布局/换行/挤压/溢出只有像素能证。
- `chrome-btn-ghost` = 纯图标按钮(28px 方块);**带文字的分段控件/ tab 用 `.chrome-tab`**,
  别再误用 ghost 类放文字。CJK 文字按钮尤其要 `white-space:nowrap`。

---

### fake backend 绕过真 PG 外键,500 留到前端真机才暴露(2.2.0 Compare,PR #71→#75)

**症状**
- Compare 后端 PR1-4 单测 + 真机测试全过、合并上生产;PR5 前端真机走查点"开始对比"
  → 500 IntegrityError:run_index 外键 fk_run_index_job_id_jobs 违约。

**根因(两层)**
1. 代码:`run_compare_task`(routes)在一个事务里**先 insert run_index(带 job_id 外键)**,
   但 jobs 行此刻还没写(job enqueue 在之后 / 走了独立连接,事务内不可见)→ 外键违约。
   修法:同一连接 conn 上先 enqueue job 再 insert run_index;PostgresJobBackend 要绑当前 conn。
2. 测试盲区:PR2 的"真机"测试用 `_FakeBackend`(假 job backend,不走真 PG 外键),
   只覆盖 worker 层,**没覆盖 routes 层 run_compare_task 的真 PG 写顺序**。所以这个外键违约
   单测、worker 真机测试都抓不到,只有"真 PG + 真 HTTP 路由"路径才暴露——拖到前端真机走查。

**修法**
- #75 修插入顺序 + 补一条 routes 级真 PG 集成测试(提交 run → 断言 jobs + run_index 都落库)。

**教训**
- **"真机测试"也分层**:用 fake/stub 替身的"集成测试"仍可能绕过真 DB 约束(外键/唯一/CHECK)。
  凡是写多表 + 有外键依赖的写路径,要有一条**真 PG + 真路由**的端到端测试,别让替身把约束架空。
- 后端能力域合并上线 ≠ 全链路验过;**接它的前端真机走查是最后一道真 DB 闸**——这也是
  "每棒前端都真机走查"的复利:它逼出了后端测试替身掩盖的约束违约。

---

## 3. 设计背景

### 1.x DataOpsStudio 的定位(原 contract §9 内容)

> **参考 1.x 时,看 `docs/legacy/V1_AS_IS.md`(1.x 现状文档,基于真实代码扫描),
> 不要直接翻 1.x 源码猜。** 该文档记录:目录结构、4 个数据库方言能力矩阵、
> 数据存储结构、核心业务规则位置、lineage 真实结构。

1.x 在 2.0 开发期**不发展、不删除、2.0 GA 后 6 个月 EOL**(见设计稿 §10.6)。
四个用途,agent 必须正确对待:

| 用途 | 怎么用 |
|---|---|
| **业务规则字典** | 写 2.0 adapter / Compare / Scenario / Lineage 时,1.x 对应实现是验证过的业务规则参考。adapter 是**移植** 1.x(MySQL/Oracle/DM/DB2 方言处理已踩过坑),不是凭空新写 |
| **迁移源** | `migrate_from_v1.py`(T8)要读 1.x 数据结构,1.x 代码是迁移逻辑依据 |
| **自用工具** | 2.0 成熟前,1.x 仍是当前可用工具,不可破坏 |
| **dogfooding 对照组** | 2.0 每个能力域做完,拿 1.x 同功能对照,验证 2.0 没退化 |

**关键边界**:
- 参考 1.x 的是**业务规则**(方言怎么处理、字段怎么映射),**不是架构**
- **架构一律以 2.0 设计稿为准**。1.x 的烂设计(5 套 execution / 明文密码 /
  ResultSet 截断 / 同进程跑长任务)正是 2.0 要修掉的,**禁止照搬**
- 写 adapter 时:查 1.x 怎么连库、怎么处理方言 → 但按 contract §3.2 的新接口组织代码
- 有冲突时:业务规则信 1.x,代码结构信设计稿

**为什么不直接 fork** —— 1.x 的耦合(Workspace = monolith,前后端杂糅)被
设计稿明确拒绝。2.0 是契约驱动的从头设计。

### Adapter 移植具体注意(写 Oracle/DM/DB2 adapter 时必读;来自 V1_AS_IS.md §2)

- **DM 方言继承 Oracle**:1.x `DmDialect(OracleDialect)` 只 override 4 个方法
  (introspect/connect 等)。推进顺序虽是 MySQL→DM→Oracle,但**写 DM 时其方言
  逻辑大量依赖 Oracle**,需先理解 Oracle 方言。这是隐含依赖
- **取消查询是"软取消",不是真 kill**:1.x 无 driver-level cancel,靠
  `cancel_requested` flag 在安全点轮询 + statement timeout 兜底。2.0
  `DatabaseAdapter.kill_query` 在 MySQL/Oracle/DM/DB2 上大概率**做不到真 kill**,
  `AdapterCapabilities.server_side_cancel` 多为 False。照 1.x 软取消模式做,
  不要假设能真 kill
- **流式读分两路**:MySQL 用 `SSCursor`(server-side unbuffered),其它驱动用
  普通 cursor + `fetchmany`。2.0 spool 拉取要按驱动区分
- **标识符大小写**:MySQL 反引号原样;Oracle/DM 用 `UPPER()` 包裹 + 双引号 quote
- **SQL guard 已有成熟实现**:1.x `app/utils/sql_guard.py:validate_readonly_sql`
  是验证过的只读 SQL 校验(首词 SELECT/WITH、拒多语句、拒 FOR UPDATE、禁词集合),
  2.0 直接移植

### Lineage 注意(2.4.0 lineage 写作时必读;来自 V1_AS_IS.md §9)

- 1.x **没有 "lineage aspect" 概念**(代码零 `aspect` 关键字)。lineage 产出是
  一个 `LineageReport`(20 字段 Pydantic)
- 2.0 lineage(2.4.0 才做)**沿用 `app/models/lineage.py` 的 LineageReport schema**,
  不要发明 aspect 体系
- "aspect" 一词只属于资产分类(asset_aspects:6 个标签类型),与 lineage 无关,
  两套独立
- lineage 解析结果不持久化(每次重算),migrate 不迁

### 三份文档定位

| 文档 | 大小 | 用途 | 冲突时优先级 |
|---|---|---|---|
| `docs/design/dataops-2.0-tech-design-v0.3.2.md` | 1806 行 | 产品 + 架构 + 接口完整设计 | **最高(以设计稿为准)** |
| `AGENT_CONTRACT_v2.md` | ~24KB | 执行契约,agent 硬约束 | 设计稿的执行子集 |
| `AGENTS.md` / `CLAUDE.md` | ~5.5KB | 宪法 + 速记,agent 自动加载 | contract 的子集 |

`AGENTS.md` 与 `CLAUDE.md` 内容除首行标题外一字一致(CI `Agents Doc Sync` 守护)。
Codex 自动加载 AGENTS.md,Claude Code 自动加载 CLAUDE.md,两者拿到同一份宪法。

### Karpathy 4 条来源

用户引用 Andrej Karpathy 关于 AI 编程的指导原则。落到 contract §0 + CORE §1。
具体出处见用户的设计沟通。

---

## 4. ADR 索引

- `docs/adr/0018-worker-heartbeat-timeout-slow-olap.md` — 600s heartbeat timeout
  取舍("误杀 > 晚恢复"),F2 正解(独立 heartbeat 线程)2.7.0 hosted scale-out 前必修

---

## 5. 当前 CI 守护清单(post T7 phase 1)

```
✓ Lint & Format & Type & AST Red Lines      R1/R2/R3/R5/R6/R10 静态检查
✓ Secret Scan (R8)                           gitleaks 全仓
✓ Unit Tests & Alembic Migration (PG 16)     单测 + R4 DB CHECK smoke
✓ MySQL Integration (R6)                     R6 hard evidence
✓ PG Queue Concurrency (T1 hard evidence)    FOR UPDATE SKIP LOCKED + reaper
✓ E2E Acceptance (contract §5 hard evidence) §5 全链路 r=2 + columns[0].name=="r"
✓ Cold-start (T6 hard evidence)              零→跑起来 8 步 r=2(apt install pg-16 + launcher)
✓ Frontend Build (T7)                        vue-tsc 严格 + vite build
```

新增 CI job 一律标"X hard evidence",形成 main 上的"红线 + 真验收"双层守护。
