# C-9 Workflow 运行通知(run 终态 → webhook / 企业微信 / 邮件) 设计稿

> 2026-07-07,Opus 4.8。方法:两路考古 —— 服务器 1.x 真源码(`~/dataops-studio/app/`,
> 只读)测绘 notifier;本仓 main 测绘 2.0 workflow 终态钩子接入面 + SecretStore / 日志脱敏范式。
> 每条带 `文件:行号` 定位,含凭据/webhook URL 的行一律跳过不记。
>
> **结论先行**:1.x 有一个完整可移植的 `services/notifier.py`(webhook / 企微 / 邮件三渠道,
> 失败隔离已做对),但**明文配置存在 workflow JSON + 环境变量**里(webhook url / smtp password
> 直接明文),不符 2.0 R2/R5。2.0 落地 = **移植 notifier 的渠道逻辑 + 消息模板 + 失败隔离**,
> 但**重做配置/密钥存储**(webhook token / smtp 密码走 SecretStore,持 SecretRef),
> **接入点复用已有的 `_write_compare_run_terminal(job, status)` 终态钩子范式**(worker 运行循环
> 三个终态分支各调一次)。首版建议只做 **webhook + 企微**(纯出站 HTTP,无 SMTP 依赖),邮件次期。

---

## 一、1.x notifier 测绘(移植依据)

单文件实现:`services/notifier.py`(187 行)。调用两处:
- `services/jobs.py:362` —— 后台调度 / 异步 job 跑完 workflow run 后
- `api/workflows.py:198` —— 同步 API `POST /api/workflows/{id}/run` 跑完后

### 1.1 入口与失败隔离(做对了,直接抄语义)

`notify_workflow_run(workflow, run, *, trigger, job_id)`(`notifier.py:27`):
- 逐 target 发送,**每个 target 的异常被 `try/except Exception` 捕获**(`notifier.py:46-57`),
  记 `logger.exception` + 落 `NotifyResult(ok=False, error=...)`,**从不向 workflow 执行路径抛**。
  docstring 明写 "Individual target failures are ... never raised to the workflow execution path"。
- 无配置 target = 静默 no-op(`notifier.py:38`)。
- 返回 `list[dict]`(每 target 的 NotifyResult),调用方不 persist,仅用于观测。

★ 这条"失败不拖垮主流程"的隔离语义是 C-9 硬要求(任务红线),1.x 已满足,2.0 照搬。

### 1.2 支持渠道

| 渠道 | 函数 | 出站方式 | 消息形态 |
|---|---|---|---|
| webhook | `_send_webhook` (`notifier.py:87`) | `urllib.request`(stdlib,POST JSON) | 原始 payload JSON |
| 企业微信 | `_send_wecom` (`notifier.py:105`) | 同上 | `{"msgtype":"markdown","markdown":{"content":...}}` 固定模板 |
| 邮件 | `_send_email` (`notifier.py:125`) | `smtplib.SMTP` + `EmailMessage` | subject `[DataOps] workflow {status}: {name}`,正文 = payload JSON |

- 类型分派大小写不敏感;企微接受 `wecom` / `wechat_work` 两个别名(`notifier.py:49`)。
- 超时统一 `DEFAULT_TIMEOUT_SECONDS=5`(env `DATAOPS_NOTIFY_TIMEOUT_SECONDS` 可覆盖,`notifier.py:16`),
  可被 target 级 `timeout_seconds` 覆盖(`notifier.py:182`)。
- **无重试**:发一次,失败即记 error 返回,不退避不重投。

### 1.3 配置形态(★ 2.0 必须重做的部分)

两个来源合并(`_targets_for`, `notifier.py:61`):
1. **每 workflow**:`workflow.notifications: list[dict]`(`models/workflow.py:80`,pydantic 自由 dict)。
   每项形如 `{type, url/to, events, enabled, timeout_seconds, method, smtp_host, username, password, tls...}`。
   `enabled=True` 才生效。
2. **全局环境变量兜底**:`DATAOPS_NOTIFY_WEBHOOK_URL` / `DATAOPS_NOTIFY_WECOM_WEBHOOK` /
   `DATAOPS_NOTIFY_EMAIL_TO`(`notifier.py:67-75`),默认只在 `events=["failed"]` 触发。
   SMTP 主机/账号/密码也从 `DATAOPS_SMTP_*` env 读(`notifier.py:126-143`)。

★ **不符 2.0 的点**:
- webhook url(企微 url 内含 `?key=<token>`)、smtp `password` **明文存 workflow JSON / env**
  (`notifier.py:142` `target.get("password")`)。2.0 R2 禁明文密钥入业务代码/DB 通配表。
- 消息 payload 是整份 JSON,含 `error` 全文,可能带敏感值;日志用 `logger.exception` 打了 `workflow.id`
  但也可能因异常携带 url。2.0 R5 禁敏感值入日志。

### 1.4 触发时机 / 终态过滤

- 触发点:**run 已跑完拿到终态 run 对象之后**(jobs / api 两处)。
- 终态过滤:`_target_accepts_event`(`notifier.py:79`)—— target 的 `events` 列表(默认 `["failed"]`),
  支持 `"all"` 或精确匹配 `run.status.value`(1.x run 状态含 success/failed 等)。
- payload 字段(`_workflow_run_payload`, `notifier.py:153`):event/trigger/job_id/workflow_id/name/
  project/owner/status/run_id/started_at/finished_at/elapsed_seconds/error/node_status_counts。

### 1.5 移植取舍小结

| 直接抄 | 重做 |
|---|---|
| 三渠道发送逻辑 + 企微 markdown 模板 | 配置存储(明文 → SecretRef + DB) |
| 失败隔离(per-target try/except,不抛) | 密钥来源(env/JSON 明文 → SecretStore.reveal) |
| 终态 events 过滤语义 | 日志(禁打 url/token;payload error 需裁剪) |
| payload 字段结构(适配 2.0 run 模型) | 出站 HTTP(stdlib urllib 可留,或统一封装) |
| 5s 超时 | 首版是否加重试(见 §三) |

---

## 二、2.0 现状(接入点 + 存储范式)

### 2.1 workflow run 终态在哪落定 —— 通知钩子接入点

2.0 workflow run 是 **job DAG 推进器**(`app/worker.py:_execute_workflow_run`, `worker.py:1185`,
ADR-0009 单 worker 串行让位模型)。run job 每次 claim 推进一步,全节点终态才决出 run 终态。
**终态在 worker 主运行循环三个分支落定**(`worker.py:426-465`):

| run 终态 | 触发 | 落定代码 | 已有的终态钩子调用 |
|---|---|---|---|
| cancelled | `JobCancelled` | `worker.py:429` `mark_cancelled` | `worker.py:432` `_write_compare_run_terminal(job, CANCELLED)` |
| failed | `WorkflowRunFailedError`(`worker.py:1270` raise)→ `Exception` | `worker.py:442` `fail` | `worker.py:445` `_write_compare_run_terminal(job, FAILED)` |
| success | `_ExecutionOutcome`(非 None) | `worker.py:464` `complete` | 无(success 分支目前不调 compare terminal) |

★ **已有范式**:`_write_compare_run_terminal(job, status)` 就是"job 到终态时挂的副作用钩子"。
C-9 照此新增 `_notify_workflow_run_terminal(job, run_status)`,在**同三处终态分支**各调一次即可:
- cancelled 分支(`worker.py:432` 旁)、failed 分支(`worker.py:445` 旁)、success 分支(`worker.py:464` 后)。
- `outcome is None`(推进一步让位,`worker.py:456`)**不是终态,不调**——这点关键,run job 会多次
  claim/requeue,只有决出终态那一次才通知。
- `JobStatus` 已含 `SUCCESS/FAILED/CANCELLED/TIMEOUT`(`domain/job.py:41-44`)。timeout 走
  `reap_stale_running_jobs`(`worker.py:380`)重排/失败路径,首版可归入 failed 语义,后续单列。

**无现成 on-complete 事件总线**:终态副作用是"在各分支手写调用"(compare terminal 就这么做),
不是发布/订阅。C-9 遵循同风格,新增一个 worker 私有方法在三分支手动调,不引入事件框架(简单优先)。

### 2.2 配置存储范式

- workflow 持久化:`workflows` 表,`dag_jsonb JSONB` 存整份 `WorkflowSpec`
  (`db/migrations/versions/0016_workflows.py:28`)。
- `WorkflowSpec`(`domain/workflow.py:146`)**frozen**,字段只有 `nodes / edges / schedule`,
  **当前无 notify 配置字段**。2.0 domain 是纯模型零 IO(R1)。
- ★ 注意 1.x 的 `workflow.notifications` 字段在 2.0 **不存在**——2.0 workflow 模型是重写的 spec,
  没有 notifications/sensors/triggers 那套。C-9 要新增配置载体。

**两种落地选项**:
- **(A) 扩 `WorkflowSpec` 加 `notifications` 字段** → 存进 `dag_jsonb`,**无需 DB 迁移**(JSONB 自由)。
  配置随 workflow 走,符合"每 workflow 通知"语义。**推荐**。
- (B) 新建 `workflow_notifications` 表 → 需迁移,支持全局/复用订阅,但首版过重。暂不做。

密钥字段:notify 配置里**不存明文 token/password**,只存 `SecretRef.ref`(见 §四.1)。

### 2.3 密钥存储范式(SecretStore)

- `SecretKind` 枚举**已含 `WEBHOOK_SECRET`**(`domain/secret.py`)—— C-9 webhook/企微 token 直接用;
  邮件 SMTP 密码可用 `WEBHOOK_SECRET` 或按需新增(如 `SMTP_PASSWORD`,需评估 R4 边界:SMTP 属
  Application Secret,可进 secret_refs,不是 Bootstrap)。
- `SecretStore` 协议(`infrastructure/secretstore/protocol.py`):`store_secret(plaintext, kind)→SecretRef`,
  `reveal_secret(ref)→plaintext`(**每次调用必审计**)。R3:Fernet/bcrypt 只在 secretstore/ 目录。
- worker 已持有 SecretStore 实例:`LocalFileSecretStore(engine, bootstrap)`(`worker.py:1799`),
  datasource 密码就是这么 reveal 的(`worker.py:1492` 读 `password_secret_ref`)。C-9 notifier 复用同一 store。
- `SecretRef`(`domain/secret.py`)是**跨存储逻辑引用**,DB 里就是普通 VARCHAR,不加 FK。

### 2.4 出站 HTTP 现状

- **仓内无统一 httpx/aiohttp/requests 封装**(pyproject/requirements 无 httpx 依赖;grep 无命中)。
- 1.x notifier 用 **stdlib `urllib.request`**。2.0 首版建议**沿用 stdlib urllib**(零新依赖,R1 友好——
  纯出站 HTTP 不碰 DB 驱动),把发送函数放 `app/services/`(或 `app/domain/notify` 若保持零 IO——
  但出站 HTTP 是 IO,应归 services/infrastructure)。若后续要连接池/重试策略,再引 httpx。

### 2.5 日志脱敏现状(R5 关键)

- structlog processor `SENSITIVE_KEY_FRAGMENTS`(`observability/logging.py:39`)按 **key 名**片段拦截:
  含 `password/secret/token/api_key/authorization/bearer/...` 的 key → REDACTED。
- ★ **`url` / `webhook` 不在拦截片段里**!企微 webhook url 形如 `...?key=<token>`,token 藏在 url value 里,
  key 名叫 `webhook_url` **不会被自动脱敏**。→ C-9 **绝不能用任何 key 打 webhook url 原值**;
  只能日志目标的 label/id 或 url 的 host 部分,token 段落必须自己截掉(见 §四.4)。

---

## 三、落地设计

### 3.1 配置模型(选项 A:扩 WorkflowSpec)

新增(存 `dag_jsonb`,无迁移):

```
class NotifyTarget(BaseModel):          # domain 纯模型
    id: str
    channel: Literal["webhook", "wecom", "email"]
    events: list[Literal["success","failed","timeout","cancelled","all"]] = ["failed"]
    enabled: bool = True
    # —— 非敏感连接信息明文存 ——
    url_secret_ref: str | None          # webhook/wecom:整条 url(含 token)存 SecretStore,这里只放 ref
    email_to: list[str] = []            # 邮件收件人(非敏感)
    smtp_host: str | None; smtp_port: int = 25; smtp_from: str | None
    smtp_user: str | None
    smtp_password_secret_ref: str | None
    timeout_seconds: int = 5

class WorkflowSpec(...):
    notifications: list[NotifyTarget] = Field(default_factory=list)   # 新增字段
```

- ★ webhook/企微的 **url 整条(含 `?key=token`)当作 secret 存**(`SecretKind.WEBHOOK_SECRET`),
  配置里只留 `url_secret_ref`。这样 DB/JSON 里没有明文 token(R2)。API 层收到明文 url →
  `store_secret` → 只落 ref。
- events 语义沿用 1.x(`all` 或精确匹配终态)。

### 3.2 渠道抽象

```
class NotifyChannel(Protocol):
    channel: str
    def send(self, target: NotifyTarget, payload: RunNotification, *, reveal) -> NotifyResult: ...
```

三实现:`WebhookChannel` / `WecomChannel` / `EmailChannel`,逻辑移植 1.x 三个 `_send_*`。
`reveal` 回调 = `secret_store.reveal_secret`,发送前一刻才解密 url/password,**不在内存长留、不落配置**。
`RunNotification` payload 参照 1.x `_workflow_run_payload`,字段适配 2.0 run 模型
(workflow_id/name/project、run job id、status、started/finished、elapsed、error 摘要、node 计数)。

`NotifyResult(channel, target_id, ok, error)` —— target 用 **id 不用 url**(R5)。

### 3.3 钩子接入(worker)

新增 worker 私有方法,仿 `_write_compare_run_terminal`:

```
def _notify_workflow_run_terminal(self, job, run_status: str) -> None:
    try:
        spec = _workflow_spec_from_payload(job.payload)
        service = WorkflowNotifyService(self._secret_store, channels)
        service.notify(spec, run_snapshot, run_status)   # 内部逐 target 失败隔离
    except Exception:
        logger.warning("workflow run notify skipped", job_id=job.id)  # 不打 url/error 明文
```

- 只对 `job.kind is JobKind.WORKFLOW_RUN` 生效(其它 job kind 不通知)。
- 三终态分支各调一次:cancelled(`worker.py:432` 旁)、failed(`worker.py:445` 旁)、
  success(`worker.py:464` 后)。`outcome is None` 让位分支不调。
- **整个方法再包一层 try/except** 兜底:即使通知服务构造/spec 解析炸了,也绝不影响已写入的 run 终态。

### 3.4 失败隔离(双层)

1. service 内 per-target `try/except`(移植 1.x `notifier.py:46`),单渠道失败不影响其它渠道。
2. worker 钩子外层 `try/except`(§3.3),整个通知子系统失败不影响 run 终态落定 / worker 循环。
   —— run 终态已在调用钩子**之前**由 `complete/fail/mark_cancelled` 写入,通知是纯 after-effect。

### 3.5 分 PR 建议

| PR | 内容 | 迁移 |
|---|---|---|
| **PR1** | domain 层:`NotifyTarget` 模型 + 扩 `WorkflowSpec.notifications` + 校验(channel 白名单、events);纯模型 + 单测 | 无(JSONB) |
| **PR2** | services 层:`NotifyChannel` 抽象 + Webhook/Wecom 两实现(stdlib urllib)+ `WorkflowNotifyService`(失败隔离);单测用 fake HTTP | 无 |
| **PR3** | worker 接入:`_notify_workflow_run_terminal` 挂三终态分支 + 外层隔离;集成测试(run 终态触发通知、通知失败不影响 run) | 无 |
| **PR4** | API + 前端:配置 CRUD(收明文 url → `store_secret` → 存 ref)、reveal 审计接入 | 视 API 而定 |
| **PR5(次期)** | `EmailChannel`(smtp)+ 可选 SMTP_PASSWORD SecretKind | 视枚举而定 |

---

## 四、范围建议(首版优先级)

1. **首版做 webhook + 企业微信**(§3.5 PR1-4):两者都是**纯出站 HTTP POST**,零 SMTP 依赖,
   企微是国内团队最常用的 run 告警通道,ROI 最高。事件默认 `failed`(最小噪音)。
2. **邮件次期**(PR5):SMTP 需额外密钥枚举 + 连接配置 + 更容易踩投递/反垃圾坑,单列。
3. **首版不做重试**:沿用 1.x 语义(发一次,失败记 error)。5s 超时。重试/退避留到有真实需求再加
   (加重试要防重复通知,复杂度上升,首版不值)。
4. **首版配置载体用选项 A**(扩 WorkflowSpec,无迁移),不建全局订阅表。

---

## 五、红线注意(必须覆盖)

- **R2(密钥只持 SecretRef)**:webhook/企微 url(含 token)、SMTP 密码**只经 SecretStore**:
  API 收明文 → `store_secret(kind=WEBHOOK_SECRET)` → 配置/DB 只存 `*_secret_ref`。
  业务代码 / `dag_jsonb` / 任何通配表**不出现明文 token/password**。发送前一刻 `reveal_secret` 取值,
  用完不缓存。
- **R5(日志/内容禁敏感值)**:
  - ★ `url`/`webhook` **不在** structlog 脱敏片段(`logging.py:39`),企微 token 藏在 url value 里,
    **必须靠人不打**:日志只出 `target_id` / `channel` / host,**绝不打 raw url**。
  - `logger.exception` 里**不带** payload / url / error 全文;失败只记 `channel + target_id + 错误分类`。
  - 通知**内容本身**(发给第三方的 payload)裁剪 `error` 字段,避免把内网连接串/密码风格文本外发。
- **R1(notifier 归 services/domain,不引 DB 驱动)**:domain 放纯模型(NotifyTarget);
  发送/HTTP/SMTP 归 `app/services/`(或 infrastructure),**不 import 数据库驱动**。
  SecretStore 通过依赖注入传入(worker 已持实例),services 不自己开 DB 连接。
- **失败隔离(任务硬红线)**:双层 try/except(§3.4)。run 终态在通知**之前**已落定;
  通知子系统任何异常都不得回传 worker 循环、不得改写 run 终态。

---

## 六、测试要求

- **domain(PR1)**:`NotifyTarget` 校验(非法 channel 拒、events 白名单、enabled 默认);
  `WorkflowSpec` 带/不带 notifications 均可序列化回 `dag_jsonb`(round-trip)。
- **channel(PR2)**:fake HTTP server / monkeypatch urllib —— 断言 webhook POST body、企微
  markdown 模板结构、超时行为、非 2xx → `ok=False`;**断言 reveal 回调被调、明文不出现在返回的
  NotifyResult / 日志**。
- **service 失败隔离**:一个 target 抛异常,其它 target 仍发送;service 不向上抛。
- **worker 接入(PR3)**:
  - run 到 failed/success/cancelled 三终态各触发一次通知(events 匹配时);`outcome is None` 让位**不**触发。
  - ★ **通知服务抛异常时,run 终态仍正确落定**(`complete/fail/mark_cancelled` 已执行),worker 循环不崩。
  - events 过滤:target 只订 `failed` 时,success run 不发。
- **R5 回归**:构造含 token 的 url,断言**日志输出不含 token/raw url**(即使 key 名不带敏感片段)。
- **R2 回归**:断言配置持久化后 `dag_jsonb` / DB 无明文 url/password,只有 `*_secret_ref`。
- **CI**:R9 不跑 SQLite —— 集成测试走 PG。

---

## 附:关键定位速查

| 事项 | 位置 |
|---|---|
| 1.x notifier 全量 | `~/dataops-studio/app/services/notifier.py`(只读) |
| 1.x 调用点 | `services/jobs.py:362`、`api/workflows.py:198` |
| 1.x 每-workflow 配置字段 | `models/workflow.py:80` `notifications` |
| 2.0 run 终态三分支 | `app/worker.py:429/442/464`(cancelled/failed/success) |
| 2.0 已有终态钩子范式 | `app/worker.py:432/445` `_write_compare_run_terminal` |
| 2.0 让位(非终态)分支 | `app/worker.py:456` `outcome is None` |
| 2.0 WorkflowSpec(加字段处) | `app/domain/workflow.py:146` |
| 2.0 workflow 持久化列 | `db/migrations/versions/0016_workflows.py:28` `dag_jsonb` |
| SecretKind.WEBHOOK_SECRET | `app/domain/secret.py` |
| SecretStore 协议 | `app/infrastructure/secretstore/protocol.py` |
| worker 持有的 store | `app/worker.py:1799` `LocalFileSecretStore` |
| 日志脱敏片段(缺 url/webhook) | `app/observability/logging.py:39` |
| JobStatus 终态枚举 | `app/domain/job.py:41-44` |
