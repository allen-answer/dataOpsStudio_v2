# PR-4b Workflow cron 定时触发器(tick 执行器)任务书

> 缺口:Workflow 域调度 **schema 已就绪**(`schedule_cron` / `schedule_enabled` 冗余列 +
> `CronSchedule` 基本格式校验 + `invalid_cron` 结构化 4xx),但 **缺周期性 tick 执行器** ——
> 没有任何后台循环去"扫描到点该触发的 workflow 并入队 run"。用户配了 cron 也不会自动跑。
> PR-4b 补这个执行器:一个进程内周期 tick,扫 `workflows.schedule_cron`,对到点的 schedule
> 复用 **PR-4 手动触发的同一条入队路径** enqueue 一个 `workflow_run` job。
> ADR-0009 §1 已拍板方案骨架(进程内 cron tick,不引 scheduler 进程 / 不引 APScheduler)。
> 本稿只做源码考古 + 落地设计,**不改任何现有文件**(只新增本任务书)。供 Codex 接手实现。

---

## 一、目标 & 验收点(§5 风格)

到点自动入队、开关生效、坏 cron 不崩、幂等不重复触发、时区确定。

| # | 验收点 | 判据 |
|---|---|---|
| A1 | **到点自动入队 run** | 一个 `enabled=true` 且 `schedule_enabled=true` 的 workflow,其 cron 命中当前 tick 窗口 → 自动 enqueue 一个 `kind=workflow_run`、`trigger="cron"` 的 job(payload 与手动触发同构),worker 正常消费执行 |
| A2 | **未到点不触发** | cron 未命中窗口 → 该 tick 不为其入队 |
| A3 | **`schedule_enabled=false` / `enabled=false` 不触发** | 两个开关任一为 false → tick 永不为其入队(`enabled` 是总开关,`schedule_enabled` 是调度开关,见 models.py:793-794) |
| A4 | **无效 cron 跳过、不崩循环** | 单个 workflow 的 cron 解析失败 → 记 structlog warning 并跳过该条,**tick 循环继续处理其余 workflow**(错误隔离,单坏 cron 不拖垮全局) |
| A5 | **幂等 / 防抖** | 同一到点窗口 tick 只入队一次;并发/重叠 tick、进程重启后重跑同一分钟,都**不产生重复 run**(靠 `last_triggered_at` 推进 + 行级锁,见 §四) |
| A6 | **时区确定** | cron 求值时区是**明确且文档化**的(首版建议全局 UTC 或单一 configured tz),同一表达式在任意部署/任意查询时刻语义一致 |
| A7 | **重启无 backfill(对齐 ADR-0009)** | API down 期间错过的窗口**不补跑**;恢复后按下一个到点窗口触发一次(ADR-0009 §1 Deferred 明确:first release no catch-up) |
| A8 | **审计留痕** | cron 触发的 run 有可区分审计(`trigger="cron"` vs `"manual"`),便于排障 |

**非目标(对齐 ADR-0009 Non-Goals):** 独立 scheduler 进程、HA 多实例分布式锁、错过窗口 backfill、秒级精度、per-workflow 自定义时区 —— 全部推迟。

---

## 二、现状测绘(带 文件:行号)

### 2.1 调度 schema(已就绪,PR-4b 只读它)

- **cron 校验**:`app/domain/workflow.py:58` `CronSchedule` —— 仅做基本格式校验(`_validate_cron:66`:5 段空格分隔 + 单字段字符集 `_CRON_FIELD_RE = ^[0-9*,\-/]+$`,workflow.py:46)。**模块 docstring(workflow.py:16)明写"不引入 croniter;精确解析属 PR-4"** —— 即到点判定所需的 cron 解析 **本任务负责引入**。
- **`invalid_cron`**:`CronSchedule._validate_cron` 抛 `ValueError("invalid_cron: ...")` → pydantic 包装 → `_workflow_spec_error`(core.py:3239)按 `_WORKFLOW_SPEC_ERROR_CODES` 归一为 400(`invalid_cron` 已在错误码集合,core.py:234)。**这是创建期校验;PR-4b 的 tick 是运行期消费,面对的是已入库、已通过基本校验的 cron 字符串**。
- **冗余列**:`app/db/models.py:778` `workflows` 表 —— `schedule_cron`(String(64), nullable, models.py:791)、`schedule_enabled`(Boolean, default false, models.py:792)、`enabled`(Boolean, default true, 总开关, models.py:794)。models.py:775-776 注释:**"冗余自 dag_jsonb.schedule,供 PR-4 调度器 tick 扫表用(避免每 tick 全表解析 JSONB)"** —— tick 扫表就吃这两列,不解 JSONB。
- **迁移**:`app/db/migrations/versions/0016_workflows.py:29-36` 建这两列。**无 `last_triggered_at` / `next_run_at` 字段**(全仓 grep 无命中)—— §四判定"到点"需要它,是本任务的迁移决策点。
- **写同步**:create/update workflow 时 `schedule_cron`/`schedule_enabled` 从 `spec.schedule` 同步写入(core.py:3100-3101、3174-3175)。

### 2.2 现有入队路径(tick 必须复用,不另造)

`app/api/routes/core.py:3323` `trigger_workflow_run`(PR-4 手动触发,§行注释明确"cron tick 属 PR-4b"):

1. 校验 `enabled`,否则 409 `workflow_disabled`(core.py:3342)。
2. **enqueue 期 R7 再校验**:`_validated_workflow_spec(dict(row["dag_jsonb"]))`(core.py:3345)—— 双门禁(创建 + enqueue,ADR-0009 Consequences)。
3. 构造 `Job(kind=JobKind.WORKFLOW_RUN, status=PENDING, ...)`(core.py:3348),payload 快照:
   ```
   {workflow_id, workflow_name, trigger:"manual", spec: spec.model_dump(),
    when_variables: builtin_when_variables(datetime.now(UTC))}
   ```
   （core.py:3359-3367;**spec 快照进 payload,执行期不回读 workflows 表**，触发后改定义不影响在途 run，core.py:3335)。
4. `_enqueue_job_txn(conn, services, job)`(core.py:3369 → 定义 3439)—— PG backend 复用当前事务连接 enqueue。
5. `_audit_business(action="workflow_run_trigger", result="accepted")`（core.py:3370）。

**worker 消费**:`app/worker.py:422` `_execute_workflow_run`(claim 到 `kind=workflow_run` 的 job 后推进 DAG)。tick **不碰执行**,只负责 step 3-4 的 enqueue —— 执行仍在 worker。

**`builtin_when_variables`**:`app/domain/workflow_when.py:64` —— 触发时刻冻结的确定性变量(today/now/year/month/day),写进 payload 供执行器与状态查询共用同一份快照(when 跨午夜不翻转)。cron 触发同样要冻结**触发时刻**。

### 2.3 周期性后台循环的现有范式

- **worker 主循环**:`app/worker.py:372` `run_forever()` → `run_once()`(worker.py:378):每轮先 `reap_stale_running_jobs(...)`(worker.py:380)再 `claim_next`,无活干则 `time.sleep(poll_interval_seconds)`。**这是"进程内周期扫表 + 让位睡眠"的现成范式**,tick 循环结构可照抄。
- **per-job 心跳线程**:`worker.py:476` `_start_job_heartbeat_thread` 用 `threading.Thread(daemon=True)` + `threading.Event` 停止 —— **"后台守护线程 + Event 优雅停"的现成写法**。
- **API 进程当前无后台线程**:`app/main.py:1-22` 只 `uvicorn.run("app.main:app")`;`app/api/app.py:20` `create_app` **无 lifespan / 无 startup 事件 / 无后台线程**。→ tick 若放 API 进程,**需要新增 lifespan 或 startup 钩子**来起线程(现在没有落点)。
- **进程拓扑**:`app/launcher.py:325-326` `up` 起两个子进程 —— `python -m app.main`(API)与 `python -m app.worker`(worker),**各一个单进程**。ADR-0009 §1:"单 API 进程是受支持拓扑,无需分布式锁"。

### 2.4 幂等 / 行锁现有范式

`app/infrastructure/jobbackend/postgres.py` 全程 `FOR UPDATE SKIP LOCKED`(postgres.py:30 类 docstring、claim_next:76/89、reap:328/365/407)。尤其 `_requeue_stale_workflow_runs`(postgres.py:317):`WITH stale AS (SELECT ... FOR UPDATE SKIP LOCKED LIMIT ...) UPDATE ... RETURNING` —— **"选中 → 加锁 → 原子更新 → 返回"的 CTE 范式**,正是 tick 去重要抄的形状(选到点的 workflow → 锁行 → 原子推进 `last_triggered_at` → 返回该入队的行)。

### 2.5 croniter 依赖现状

`pyproject.toml` **无 croniter**(grep 仅命中 `uvicorn`)。到点判定要么引 croniter,要么自研 cron matcher。见 §四决策。

### 2.6 R7 与本功能

- R7 白名单一致性单测已在 CI(ADR-0009 §Context;`tests/unit/test_redlines.py`)。
- **本功能不新增/改动节点 kind**,只入队 `workflow_run`(它本身**不在**白名单、也不该在 —— 无嵌套,ADR-0009 §Node whitelist)。
- **但 tick enqueue 必须复用 `_validated_workflow_spec` 做 enqueue 期 R7 再校验**(与手动触发对齐,ADR-0009 双门禁):存量 workflow 的 dag 理论上创建期已过校验,但 enqueue 期再验是契约要求,且防"白名单收紧后存量 workflow 变非法"。

---

## 三、实现方案

### 3.0 核心决策一览

| 维度 | 决策 | 依据 |
|---|---|---|
| tick 落哪个进程 | **API 进程内后台守护线程**(ADR-0009 §1 已拍板) | ADR-0009 §1;单 API 进程拓扑无需分布式锁 |
| 起线程的钩子 | 新增 FastAPI **lifespan**(app.py:20 `create_app` 目前无) | 2.3 |
| 到点判定 | 引 **croniter** + 新增 **`last_triggered_at`** 列 | 2.1 docstring 已把 croniter 归给 PR-4;2.5 |
| 去重/幂等 | `last_triggered_at` 单调推进 + `FOR UPDATE SKIP LOCKED` 事务原子更新 | 2.4 |
| 复用入队 | 抽 `trigger_workflow_run` 的 Job 构造 + enqueue 为**共享函数**,tick 与手动触发共用 | 2.2 |
| 时区 | 首版**单一确定时区**(建议 UTC 或 config `scheduler_timezone`),per-workflow tz 推迟 | A6;2.2 时间戳皆 UTC |
| backfill | **不补跑**,错过窗口丢弃,恢复后下一窗口触发一次 | ADR-0009 §1 Deferred |

### 3.1 tick 循环放哪(进程模型)

**遵循 ADR-0009 §1:API 进程内轻量后台守护线程。**

- 在 `create_app`(app.py:20)加 **lifespan**:startup 起 `SchedulerTick` 线程(`threading.Thread(daemon=True)`,照抄 worker.py:476-493 的 Event 停止范式);shutdown `event.set()` + `join(timeout=...)`。
- 线程体是 `while not stop.wait(tick_interval): self._tick_once()` —— 结构照抄 worker `run_forever`(worker.py:372)/ 心跳线程(worker.py:487)。
- tick 只做**扫表 + 判定 + enqueue**,不执行 DAG;执行仍在 worker 进程(职责不越界)。
- **需要 `ApiServices`**(engine / job_backend / 审计),lifespan 里 `app.state.services` 已有(app.py:37),线程持有其引用即可。

> **备选(需 Codex 与 owner 确认):** 把 tick 塞进 **worker 的 `run_once`**(worker.py:378,像 `reap_stale_running_jobs` 一样每轮扫一次)。优点:worker 已有成熟轮询循环 + 让位睡眠 + 单进程,零新钩子,且 enqueue 本质是写队列(worker 天然干这个)。**缺点:与 ADR-0009 §1 "API 进程内 tick" 的字面决策相悖。** 本稿按 ADR 走 API 进程;但 worker-loop 方案更省事、风险更低,**若 owner 松口应优先考虑**(列入 §六 待确认)。

### 3.2 扫描频率

- 新增 config key `scheduler_tick_seconds`,**默认 30s**(§6.A#5 已定;cron 最细粒度是分钟,30s 稳妥不漏窗口边界)。**并入现有 `ApiConfig`**(不新开 `SchedulerConfig` 类,§6.A#5)。
- 附一个总开关 `enabled: bool = True`,便于测试/特定部署关闭 tick。

### 3.3 如何判定"到点"(关键)

**方案:`last_triggered_at` + croniter `get_prev`/`get_next`。**

新增列 `workflows.last_triggered_at`(timezone-aware, nullable)。每个 tick,对每条候选 workflow:

```
seed = last_triggered_at or <workflow 生效基准，见下>
next_fire = croniter(cron, seed).get_next(datetime)   # seed 之后的首个到点
if next_fire <= now:            # 到点了
    在同一 FOR UPDATE 事务里：
      重新确认 last_triggered_at 未被并发 tick 推进（乐观/行锁）
      enqueue workflow_run（复用 3.5）
      UPDATE last_triggered_at = now（或 = next_fire，见下"防连炮"）
```

- **首次触发的 seed(`last_triggered_at IS NULL`)**:用 workflow 的某个基准锚(建议 `updated_at`,或迁移时回填 `now()`)。**关键:不能用固定过去时刻做 seed**,否则 croniter 会认为"从很久以前到现在错过了一堆",在无 backfill 语义下应只看"当前窗口是否到点"。→ 简化:seed 缺失时,**首个 tick 直接把 `last_triggered_at` 设为 now(不入队),下一窗口起正常判定**;或用 `get_prev(now)` 判断"上个到点是否落在 (seed, now]"。落地时二选一并写清(§六)。
- **防连炮 / 单调**:`last_triggered_at` 只增不减,推进后同一分钟再 tick,`next_fire = croniter(seed=last_triggered_at).get_next()` 已 > now → 不重复(满足 A5)。
- **候选集查询**(利用冗余列,不解 JSONB):`SELECT ... FROM workflows WHERE enabled AND schedule_enabled AND schedule_cron IS NOT NULL`(satisfies A3)。建议加部分索引 `WHERE schedule_enabled`(表小可先不加,§六)。

### 3.4 是否需要 DB 迁移 —— **需要**

**新增迁移 `00XX_workflow_last_triggered_at.py`**(down_revision 接当前 head,Codex 落地时确认链号):

- `workflows.last_triggered_at` `DateTime(timezone=True)`, **nullable**(存量行为 NULL,首触发按 3.3 seed 逻辑)。
- 同步更新 `app/db/models.py:778` `workflows` Table 定义 + `tests/unit/test_models.py`(该文件已断言 `schedule_cron` 等列,test_models.py:265/281)。
- （可选)部分索引 `ix_workflows_due (schedule_enabled) WHERE schedule_enabled AND enabled` —— 表规模小可推迟,列入 §六。

> 这是本任务**唯一必需的 schema 变更**。若采用"窗口法"(§六备选,不落 `last_triggered_at`,靠 (prev_tick, now] 区间判定)则可免迁移,但**去重/重启幂等更脆**(进程内内存态 prev_tick 一重启就丢),故本稿主推 `last_triggered_at` 落库方案。

### 3.5 如何复用现有入队路径

**抽公共函数**(外科手术,别复制粘贴):把 `trigger_workflow_run`(core.py:3348-3369)里"构造 Job + `_enqueue_job_txn`"抽成

```
_build_workflow_run_job(*, workflow_id, workflow_name, spec, trigger, now) -> Job
```

手动触发传 `trigger="manual"`,tick 传 `trigger="cron"`。两者都:
1. `_validated_workflow_spec(dag_jsonb)`(enqueue 期 R7 再校验,2.6);
2. `when_variables = builtin_when_variables(触发时刻)`(冻结,2.2);
3. `timeout_seconds = sum(node.timeout_seconds) + 600`(core.py:3347);
4. `_enqueue_job_txn(conn, services, job)`(事务内,core.py:3439)。

tick 的 enqueue **必须与 `last_triggered_at` 的 UPDATE 在同一事务**(原子:要么"入队 + 推进时间戳"一起成,要么都不成),否则崩溃点会漏触发或重复触发。

### 3.6 并发 / 幂等(避免一个 tick 触发两次)

- **单进程内**:tick 是单线程串行扫表,天然无自我并发。
- **跨 tick / 跨进程兜底**:即便未来误起两个 API 进程,`FOR UPDATE SKIP LOCKED` 选中候选行 + 同事务 `UPDATE last_triggered_at`(2.4 `_requeue_stale_workflow_runs` 范式)保证同一窗口只有一个 tick 抢到并推进,另一个看到已推进的时间戳 → 不重复(满足 A5)。
- 首版按 ADR 只支持单 API 进程,行锁是**纵深防御**,非分布式锁(不承诺 HA)。

### 3.7 错误隔离(单坏 cron 不拖垮循环)

- **每条 workflow 独立 try/except**:croniter 解析失败 / enqueue 失败 → 捕获、structlog `logger.warning("scheduler skip workflow", workflow_id=..., reason=...)`(**不打印 cron 全文以外的敏感值**,见 §五 R5)、`continue` 下一条。**绝不让单条异常冒泡杀死 tick 线程**(满足 A4)。
- **tick 线程顶层再包一层 try/except**:任何未预期异常 → 记 error 日志,**睡一个 interval 后继续下一轮**,线程不退出(参照 worker 心跳线程 worker.py:487-495 的"记日志不崩"处理)。
- 到期候选逐条处理,单条 enqueue 事务失败不影响其余条。

### 3.8 时区处理

- DB 时间戳全 UTC(`now()` / `datetime.now(UTC)`,2.2)。croniter 求值**必须显式带 tz**(否则 naive/aware 混用出错)。
- **首版建议:全局单一时区**,config `scheduler_timezone`(默认 `UTC`,或部署方设为 `Asia/Shanghai`)。cron 表达式按该 tz 求值,`last_triggered_at` 存 UTC。写清"cron 按 X 时区解释"给用户。
- **per-workflow 时区推迟**(schema 无 tz 字段,加会牵动 CronSchedule + 迁移;非本任务范围,列 §六 + 非目标)。

---

## 四、分步实施建议(可拆小 PR)

| PR | 范围 | 可独立合? |
|---|---|---|
| **4b-1** | **迁移 + 模型**:`workflows.last_triggered_at` 列 + models.py Table + test_models.py 断言;加 croniter 到 pyproject | 是(纯 schema/依赖,零行为) |
| **4b-2** | **入队路径抽公共函数**:`_build_workflow_run_job`,`trigger_workflow_run` 改为调用它(手动触发行为不变,回归测试保证);纯重构 | 是(外科手术,不改对外行为) |
| **4b-3** | **tick 核心 + 到点判定纯函数**:`app/domain/scheduler.py`(纯函数 `due_workflows(rows, now, tz) -> list[due]`,croniter 求值,**零 IO / 零 DB**,R1 安全)+ 单测(§五)。这一层可先落、可独立测 | 是(纯 domain,测试友好) |
| **4b-4** | **tick 线程 + lifespan 接线**:`SchedulerTick`(持 ApiServices,扫表 → 调 4b-3 判定 → 4b-2 入队 + 推进 `last_triggered_at`,同事务)+ `create_app` lifespan 起停线程 + `SchedulerConfig` | 依赖 4b-1/2/3 |
| **4b-5**(可选) | 部分索引 + 可观测(tick 计数/最近触发的轻量指标或日志)+ 前端"下次触发时间"展示(若需要) | 可推迟 |

**最小可用切分**:4b-1 → 4b-2 → 4b-3 → 4b-4 四个即闭环。4b-3 的纯 domain 判定函数是测试重心,建议先行。

---

## 五、红线注意 + 测试要求

### 红线(R1–R10)

- **R1（`app/services/` 禁 import DB 驱动)**:到点判定纯函数放 `app/domain/scheduler.py`(只依赖 croniter + datetime,**零 DB**)。扫表/enqueue 的 IO 部分放 API 层(`app/api/`)或 backend 层,**不放 `app/services/`,不在 domain 层碰 engine**。croniter 是纯计算库,不碰 DB 驱动。
- **R5(日志禁敏感值 + 强制 structlog,禁 stdlib logging)**:tick 日志用 **structlog `logger`**(照 worker.py 全程 structlog)。日志字段限 `workflow_id` / `cron 表达式` / `reason` / `run_id` 等**非敏感标识**;**绝不打印 workflow 内容 / SQL / 凭据 / spec 全文**。坏 cron 的 warning 只记表达式与错误类型。
- **R7（Workflow 节点白名单,2.4.0 域生效)**:tick enqueue **必须走 `_validated_workflow_spec`** 做 enqueue 期再校验(2.6;与手动触发双门禁对齐)。不新增/改动节点 kind。
- **R10(禁 f-string 拼 uuid)**:run/audit id 用现成 `new_id()`(core.py:3346 范式),禁 `f"{uuid4().hex}"`。
- **R2/R3/R8**:本功能不引入任何明文机密 / 不碰 Fernet/bcrypt/hazmat;不新增密码/token 面。

### 测试要求(单测覆盖,对齐 §一验收点)

放 `tests/unit/test_scheduler.py`(判定纯函数)+ `tests/contract/` 或 `tests/integration/`(tick 端到端入队,参照 `tests/contract/test_api.py` workflow 用例 test_api.py:2702 起 与 `tests/integration/test_workflow_persist_pg.py`)。

| 用例 | 覆盖验收点 |
|---|---|
| 到点(cron 命中窗口) → 判定 due / 入队一次 | A1 |
| 未到点(cron 不命中) → 不 due / 不入队 | A2 |
| `schedule_enabled=false` → 不入队;`enabled=false` → 不入队(两开关各一例) | A3 |
| 无效 cron(绕过创建校验、直接入库的坏值)→ 跳过 + 不抛,循环继续处理后续 workflow | A4 |
| 幂等:同一到点窗口连续两次 tick → 只入队一次(`last_triggered_at` 推进后第二次不重复) | A5 |
| 进程重启/重跑同分钟(seed=已推进的 last_triggered_at)→ 不重复 | A5 |
| 时区:同一 cron 在配置 tz 下命中时刻确定 | A6 |
| enqueue 的 job payload `trigger=="cron"` 且结构与手动触发同构(spec 快照 + when_variables) | A1/A8 |
| 入队 + `last_triggered_at` 更新同事务原子(模拟 enqueue 失败 → 时间戳不推进) | A5/A7 |

判定纯函数用**注入 `now`**(不依赖真实时钟,确定性),照 `builtin_when_variables(now)`(workflow_when.py:64)与执行器"注入 now"的既有范式。

---

## 六、决策(owner 委托,已定)+ 落地时验证项

> 下述 §6.A 是 owner 委托后拍定的实现取向,Codex 直接照做,**不必再问**;
> §6.B 是纯技术性、只能在落地当刻确定的项(如迁移 head 号),Codex 实现时核实即可。

### 6.A 已决策(照此实现)

1. **tick 进程归属 → API 进程内 lifespan 守护线程(遵 ADR-0009 §1)。**
   ADR-0009 §1 是已记录的架构决策,**默认遵守,不擅自偏离**(worker-loop 虽更省事,但推翻 ADR 属治理动作,应走 ADR 修订而非在任务书里绕过)。§3.6 的 `FOR UPDATE SKIP LOCKED` + `last_triggered_at` 幂等已让"API 多副本同时 tick"也安全,故 API 进程方案无并发隐患。
   —— 若未来确实想换 worker-loop,**先提 ADR-0009 修订** 再改,不在本 PR 内偷偷切。
2. **首触发 seed 语义 → 方案 (a):`last_triggered_at IS NULL` 时,首个 tick 只把它设为 `now`、不入队。**
   语义保守、无意外补触发:开启调度/新建 workflow **不当场触发**,从下一个 cron 窗口起正常。
3. **时区口径 → 服务器本地时区求值,时间戳一律 tz-aware UTC 落库。**
   cron 作者写 `0 2 * * *` 期望的是"本地 2 点",用本地时区求值最不意外。暴露 config key `scheduler_timezone`(默认 = 系统本地时区)以备将来需要显式指定。
   **落地前 Codex 用 `ssh daily-server` 核一下 1.x 调度用的时区**(多半就是服务器本地);若一致,默认值即对齐迁移用户预期,无需改动。
4. **croniter → 引入(MIT、纯 Python、无重依赖)。** 不自研 matcher。落地把 croniter 加进 pyproject + 锁文件(uv.lock),对齐现有依赖引入方式。
5. **tick 间隔 → 默认 30s。** cron 分钟粒度下 30s 稳妥不漏窗口边界。config key `scheduler_tick_seconds`,**并入现有 config 模块**(ApiConfig 所在处),不新开 `SchedulerConfig` 类,避免配置类膨胀。
6. **候选索引 → 首版全表扫 + `WHERE schedule_enabled`,不加部分索引。** `workflows` 表每部署仅几十条,全扫无压力;规模上来再补索引。
7. **审计动作命名 → 复用现有 `workflow_run_trigger` + detail `{trigger:"cron"}`,不新增 action。** 保持审计查询口径稳定,前端/查询无需适配新 action。
8. **多 worker / HA → 维持 ADR-0009 Deferred。** 首版单进程,行锁作纵深防御;将来多实例另行评估,不在本 PR 承诺分布式锁。

### 6.B Codex 落地当刻核实(纯技术项)

1. **迁移链 head 号**:落地时 `alembic heads` 取真实 head,新迁移 `down_revision` 接之(不硬编码 `0016` 之后的号)。
2. **1.x 时区核对**(见 6.A#3):`ssh daily-server` 确认 1.x 调度时区,回填/确认 `scheduler_timezone` 默认值。
3. **croniter 版本 pin + uv.lock 同步**(见 6.A#4):确认 CI 锁文件更新、无解析告警。
