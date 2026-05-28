# DataOpsStudio 2.0 Backlog

> review 过程中 surfaced 的"暂不阻塞、但要记得"项。每一条标明**最迟修复版本**和**触发条件**;到该版本前必修的标 ★ 必修。

## 来自 T1 (JobBackend + worker) review

### ★ **T4 前必修** — `is_cancel_requested` 每行 PG 查询

**位置**:`app/worker.py:_check_cancel`,`app/dbclients/mysql_adapter.py:_check_cancel`

**现状**:worker 在 SQL 流式 fetch 每行 / 每 fetchmany 批次都调 `backend.is_cancel_requested(job_id)` → 一次 `SELECT * FROM jobs WHERE id=?`。1M 行 = 1M+2k PG round-trip,放大查询时延。

**为什么 T4 前必修**:T4 是 API + middleware。API 上线后真用户跑 SQL,百万行结果 + 每行 cancel SQL 会显著放大延迟,可能让 SQL workspace 体验劣化(用户感知响应慢)。T1 阶段没真流量看不出来,T4 阶段必须先处理。

**修法**(任选):
1. **每 N 行检查**:N 默认 5000(payload 决定),在 fetch loop 内累计行数 / time 触发(简单)。
2. **TTL 缓存**:`_last_check_at` + `_last_result`,每 5s 才查一次 PG。期间命中缓存(响应取消 ≤ 5s)。
3. **PG NOTIFY**:cancel 由 `request_cancel` 发 `pg_notify('cancel_$job_id', '1')`,worker LISTEN(架构变大,T4 后可考虑)。

**Trade-off**:N/TTL 越大 throughput 越好但 cancel 响应越慢。1-5s cancel 响应对用户体验足够。

**关联**:ADR-0018 "Worker Heartbeat Timeout Favors Slow OLAP Queries" 的 Backlog 段也记了同一项。

---

### **T1 后随时** — `UnsupportedJobKindError` 复用语义不一致

**位置**:`app/worker.py:121` 和 `app/worker.py:240`

**现状**:同一异常类两处 raise:
- L121:`Unsupported job kind`(JobKind 不在 2.0.0 支持范围 —— 2.0.0 只 sql_query)
- L240:`Unsupported datasource db_type`(adapter factory 不支持的 DB 方言)

**问题**:两个不同语义共用一个 exception name,catch 不能区分。

**修法**:拆 `UnsupportedDbTypeError(RuntimeError)`。L240 改用新异常。

**优先级**:不阻塞,不影响功能,只影响 future error handling 清晰度。可与 T4/T5 顺手做。

---

### **F2 正解** — Worker 独立心跳线程(取消 OLAP 取舍)

**位置**:`app/worker.py:WorkerRunner`

**现状**:heartbeat 只在 fetch loop 内每 15s 触发,慢首行 OLAP 期间不触发。2.0.0 通过把 `worker_heartbeat_timeout` 调到 600s 缓解(见 ADR-0018):**避免误杀合法慢查询**优先于 **worker 死亡后更快恢复**。

**Trade-off 痛点**:600s = 10 分钟,实际 worker 崩溃后需 10 分钟才 reaper 接管。对生产场景偏长。

**正解**:独立 heartbeat 线程/定时器,与 fetch loop 解耦。线程每 15s 强制 heartbeat,即使主线程在 driver-level 阻塞首行返回。

**触发条件**:
- 客户 hosted 形态出现"worker 死了但 reaper 10 分钟才恢复"投诉
- 或 hosted 上需要把 timeout 压回 90s 提升故障恢复速度(2.6.0 hosted Copilot 上线后)

**优先级**:不阻塞 2.0.0 / 2.0.x,**2.7.0 hosted scale-out 前必修**(那时 worker fleet 规模化,单 worker 死 10 分钟意味着可见的吞吐损失)。

**关联**:ADR-0018 已记录此 trade-off + 引用本 backlog 项。

---

## Backlog 维护规则

1. **每个 review 反馈**判断三类:
   - 阻塞合并 → PR 阶段 Codex 改
   - 不阻塞但 < 1-2 个版本必修 → 进本 backlog,标明"X 前必修"
   - 长期改进 / 不确定 → 进 backlog 但不标版本
2. **本 backlog 不是 wiki** —— 修完即从此文件删除,而不是改 status。git 历史保留。
3. **每个 sprint / 每次合 T*** 之前,扫一眼此文件,确认到期项已完成。
4. 关联 ADR / design doc 章节时,**两边都写交叉引用**(像 F2/F3 与 ADR-0018 已做)。
