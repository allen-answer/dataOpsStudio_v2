"""Workflow cron 定时触发(schedule tick)—— PR-4b / ADR-0009 §1。

进程内后台线程(在 API 进程里,非独立调度进程、非 APScheduler)周期扫
``workflows.schedule_cron``,到期即复用手动触发的入队路径 enqueue 一个
``workflow_run`` job(执行仍在 worker)。ADR-0009 §1 拒绝的是 APScheduler
那种"独立调度进程 + 依赖",不是 cron 表达式解析器本身——这里用 croniter
只做纯解析(求上一触发点),tick 循环自写。

幂等 / misfire 语义(ADR-0009 §1"first release fires once, no backfill"):

- 每个 workflow 用 ``schedule_last_fired_at`` 列作防重锚点(权威游标)。
- tick 对每个到期调度算 ``most_recent_fire(cron, now)``(<= now 的最近触发点):
  仅当它 **严格晚于** ``schedule_last_fired_at`` 才入队,并把锚点原子推进到该点。
  → 同一触发点绝不重复入队(哪怕 tick 间隔 < cron 周期)。
- 进程重启不补跑历史错过点:重启后锚点仍在,停机期间错过的多个触发点只会
  被"最近触发点"这一个值代表,**至多补触发一次**(下一 tick),不逐点回灌。
- 新调度 / 旧行 ``schedule_last_fired_at IS NULL``:首次见到只**播种基线**
  (锚点设为当前最近触发点)而**不触发**,避免为"调度登记之前"的历史点误触发。
- 原子认领:推进锚点用带守卫的条件 UPDATE(``rowcount==1`` 才算认领成功),
  单 API 实例本无竞争,此守卫顺带兜住 HA 多实例误重复(HA 去重仍属后置)。

R5:全程 structlog,只记 workflow_id / run_id / 触发点时间等非敏感标识。
R1:本模块不 import 数据库驱动(psycopg 在 infrastructure 层);SQLAlchemy Core
+ ``PostgresJobBackend``(基础设施)是允许的编排依赖。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter
from sqlalchemy import or_, select, update
from sqlalchemy.engine import Connection

from app.db.models import workflows
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.workflow import WorkflowSpec
from app.domain.workflow_when import builtin_when_variables
from app.infrastructure.jobbackend.postgres import PostgresJobBackend

if TYPE_CHECKING:
    from app.api.services import ApiServices

logger = structlog.get_logger(__name__)
_UTC_TIMEZONE = ZoneInfo("UTC")


def _new_id() -> str:
    # R10:不 f-string 拼 uuid;plain str(uuid4()) 与 app.api.services.new_id 同款。
    return str(uuid4())


def most_recent_fire(
    cron_expr: str,
    now: datetime,
    *,
    timezone: ZoneInfo = _UTC_TIMEZONE,
) -> datetime | None:
    """<= ``now`` 的最近 cron 触发点(按 ``timezone`` 求值,返回 aware UTC)。

    表达式不可解析返回 ``None``(创建期已 croniter 校验,运行期这里再兜一道)。
    croniter ``get_prev`` 取严格早于基点的触发点;传入 ``now`` 时,秒级精度下
    正好落在整分触发点的边界一秒内不触发只会延后至多一个 tick(ADR-0009:重启/
    触发至多延一个 tick),与幂等锚点语义一致,不影响正确性。
    """
    if not croniter.is_valid(cron_expr):
        return None
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    itr = croniter(cron_expr, now_utc.astimezone(timezone))
    prev = itr.get_prev(datetime)
    if not isinstance(prev, datetime):  # 防御:croniter 类型宽松
        return None
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone)
    return prev.astimezone(UTC)


class ScheduleAction(Enum):
    SKIP = "skip"  # 未到期 / 已触发过 / 被禁用 / cron 不可解析
    SEED = "seed"  # 首次见到:播种基线,不触发
    FIRE = "fire"  # 到达新触发点:入队 workflow_run


@dataclass(frozen=True)
class ScheduleDecision:
    action: ScheduleAction
    fire_at: datetime | None


def evaluate_schedule(
    *,
    enabled: bool,
    schedule_enabled: bool,
    cron: str | None,
    now: datetime,
    last_fired: datetime | None,
    timezone: ZoneInfo = _UTC_TIMEZONE,
) -> ScheduleDecision:
    """纯决策(无 IO,单测核心):给定调度状态 → SKIP / SEED / FIRE。

    SQL 端已按 enabled/schedule_enabled/cron-not-null 预筛;此处再判一遍,
    既让防重/到期/播种逻辑可脱离 DB 单测,也对预筛之外的调用兜底。
    """
    if not enabled or not schedule_enabled or not cron:
        return ScheduleDecision(ScheduleAction.SKIP, None)
    fire_at = most_recent_fire(cron, now, timezone=timezone)
    if fire_at is None:
        return ScheduleDecision(ScheduleAction.SKIP, None)
    if last_fired is None:
        return ScheduleDecision(ScheduleAction.SEED, fire_at)
    if last_fired.tzinfo is None:
        last_fired = last_fired.replace(tzinfo=UTC)
    else:
        last_fired = last_fired.astimezone(UTC)
    if fire_at > last_fired:
        return ScheduleDecision(ScheduleAction.FIRE, fire_at)
    return ScheduleDecision(ScheduleAction.SKIP, None)


def build_workflow_run_job(
    *,
    workflow_id: str,
    workflow_name: str,
    project_id: str,
    owner_user_id: str,
    spec: WorkflowSpec,
    trigger: str,
    now: datetime,
    extra_variables: Mapping[str, object] | None = None,
) -> Job:
    """构造一个 ``workflow_run`` job(手动触发与 cron tick 的单一入队构造点)。

    变量合并优先级 builtin < spec.variables < 触发时(cron tick 无触发时变量),
    在 ``now`` 冻结进 payload —— 执行器每步推进与状态查询共用这份快照,when
    决策 + ``${var}`` 插值在 run 生命周期内确定(跨午夜不翻转)。
    """
    extra = extra_variables or {}
    merged_variables: dict[str, object] = {
        **builtin_when_variables(now),
        **spec.variables,
        **extra,
    }
    timeout_seconds = sum(node.timeout_seconds for node in spec.nodes) + 600
    return Job(
        id=_new_id(),
        kind=JobKind.WORKFLOW_RUN,
        status=JobStatus.PENDING,
        owner_user_id=owner_user_id,
        project_id=project_id,
        datasource_ids=[],
        # run job 优先级 -1(低于交互 sql_query 的 0),与手动触发一致。
        priority=-1,
        timeout_seconds=timeout_seconds,
        resource_profile=ResourceProfile(timeout_seconds=timeout_seconds),
        audit_id=_new_id(),
        payload={
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "trigger": trigger,
            "spec": spec.model_dump(mode="json"),
            "when_variables": merged_variables,
        },
    )


# ── C-10 SQL sensor 触发(数据到达触发 + 冷却期)──────────────────────────────
# 调度线程周期扫 sensor_enabled 的 workflow;检查间隔到期即派一个 sensor 检查 job
# 给 worker(SQL 在 worker 连库执行,不在 API 进程,遵守 R1)。worker 侧 SQL 结果
# 第一行第一列 truthy → 带冷却期守卫的原子推进 sensor_last_triggered_at + 入队
# workflow_run(经 build_workflow_run_job,与手动/cron 单一构造点)。
#
# 职责切分:调度器管**检查间隔节流**(sensor_last_checked_at 锚点,派发即推进)与
# **冷却期跳过优化**(冷却期内不派检查,省一次无谓 SQL);worker 侧的带守卫原子
# 推进是**冷却期的权威闸门**(即便两次检查因 SQL 慢于间隔而并发在途,也只触发一次)。


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SensorAction(Enum):
    SKIP = "skip"  # 未到检查间隔 / 冷却期内 / 被禁用
    CHECK = "check"  # 检查间隔到期:派 sensor 检查 job(SQL 真值判定在 worker)


@dataclass(frozen=True)
class SensorDecision:
    action: SensorAction


def evaluate_sensor(
    *,
    enabled: bool,
    sensor_enabled: bool,
    now: datetime,
    last_checked: datetime | None,
    check_interval_seconds: int,
    last_triggered: datetime | None,
    cooldown_seconds: int,
) -> SensorDecision:
    """纯决策(无 IO,单测核心):是否派发一次 sensor 检查 → SKIP / CHECK。

    - 禁用(workflow 总开关关 / sensor 关)→ SKIP。
    - 冷却期内(距上次触发 < cooldown)→ SKIP:省一次无谓 SQL(worker 侧守卫是权威闸门)。
    - 距上次检查 < check_interval → SKIP:检查间隔节流(派发即推进锚点)。
    - 否则 → CHECK。
    """
    if not enabled or not sensor_enabled:
        return SensorDecision(SensorAction.SKIP)
    triggered = _as_aware_utc(last_triggered)
    if triggered is not None and now - triggered < timedelta(seconds=cooldown_seconds):
        return SensorDecision(SensorAction.SKIP)
    checked = _as_aware_utc(last_checked)
    if checked is not None and now - checked < timedelta(seconds=check_interval_seconds):
        return SensorDecision(SensorAction.SKIP)
    return SensorDecision(SensorAction.CHECK)


def build_sensor_check_job(
    *,
    workflow_id: str,
    workflow_name: str,
    project_id: str,
    owner_user_id: str,
    datasource_id: str,
    sensor_sql: str,
    cooldown_seconds: int,
    spec: WorkflowSpec,
    now: datetime,
) -> Job:
    """构造一个 sensor 检查 job(单一构造点)。

    payload 冻结 spec 快照:worker 侧条件为真时用它经 build_workflow_run_job 入队
    workflow_run(run 反映派检查时刻的定义,不回读 workflows 表)。job 优先级 -1
    (与 workflow_run / 手动触发同档,低于交互 sql_query,不压制交互查询)。
    """
    timeout_seconds = 300  # sensor SQL 应是轻量条件查询;超时按 300s 兜底
    return Job(
        id=_new_id(),
        kind=JobKind.WORKFLOW_SENSOR_CHECK,
        status=JobStatus.PENDING,
        owner_user_id=owner_user_id,
        project_id=project_id,
        datasource_ids=[datasource_id],
        priority=-1,
        timeout_seconds=timeout_seconds,
        resource_profile=ResourceProfile(timeout_seconds=timeout_seconds),
        audit_id=_new_id(),
        payload={
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "datasource_id": datasource_id,
            "sensor_sql": sensor_sql,
            "cooldown_seconds": cooldown_seconds,
            "spec": spec.model_dump(mode="json"),
        },
    )


def _enqueue_in_txn(conn: Connection, job: Job) -> None:
    """事务内 enqueue(与 core._enqueue_job_txn 同款:PG backend 复用当前连接,
    保证认领锚点与入队 job 同一事务,失败一起回滚)。API 进程恒用 PostgresJobBackend。"""
    PostgresJobBackend(conn).enqueue(job)


def _locked_workflow_execution_row(
    conn: Connection,
    workflow_id: str,
) -> dict[str, object] | None:
    row = (
        conn.execute(
            select(
                workflows.c.id,
                workflows.c.project_id,
                workflows.c.name,
                workflows.c.created_by,
                workflows.c.dag_jsonb,
            )
            .where(workflows.c.id == workflow_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


class WorkflowScheduler:
    """进程内 cron tick 后台线程。start() 于 API 启动,stop() 于关闭。"""

    def __init__(
        self,
        services: ApiServices,
        *,
        tick_interval_seconds: float,
        timezone: ZoneInfo = _UTC_TIMEZONE,
    ) -> None:
        self._services = services
        self._interval = tick_interval_seconds
        self._timezone = timezone
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dataops-workflow-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("workflow scheduler started", tick_interval_seconds=self._interval)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
        logger.info("workflow scheduler stopped")

    def _run(self) -> None:
        # 先等一个 interval 再首扫:避免多进程/重启瞬间的启动抖动。
        while not self._stop.wait(self._interval):
            try:
                self.tick(datetime.now(UTC))
            except Exception as exc:  # 单次 tick 失败不能拖垮线程
                logger.warning(
                    "workflow scheduler tick failed",
                    error_type=type(exc).__name__,
                )

    def tick(self, now: datetime) -> None:
        """扫一遍到期调度并触发(公开供确定性单测注入 ``now``)。

        两条独立触发路径:cron(schedule)与 SQL sensor;逐行隔离,单行/单路径失败
        不影响其他 workflow。
        """
        for row in self._candidate_rows():
            workflow_id = str(row["id"])
            try:
                self._process_row(row, now)
            except Exception as exc:  # 单行失败不影响其他 workflow
                logger.warning(
                    "workflow schedule tick row failed",
                    workflow_id=workflow_id,
                    error_type=type(exc).__name__,
                )
        for row in self._sensor_candidate_rows():
            workflow_id = str(row["id"])
            try:
                self._process_sensor_row(row, now)
            except Exception as exc:  # 单行失败不影响其他 workflow
                logger.warning(
                    "workflow sensor tick row failed",
                    workflow_id=workflow_id,
                    error_type=type(exc).__name__,
                )

    def _candidate_rows(self) -> list[dict[str, object]]:
        stmt = select(
            workflows.c.id,
            workflows.c.project_id,
            workflows.c.name,
            workflows.c.schedule_cron,
            workflows.c.schedule_enabled,
            workflows.c.enabled,
            workflows.c.created_by,
            workflows.c.dag_jsonb,
            workflows.c.schedule_last_fired_at,
        ).where(
            workflows.c.enabled.is_(True),
            workflows.c.schedule_enabled.is_(True),
            workflows.c.schedule_cron.isnot(None),
        )
        with self._services.engine.connect() as conn:
            return [dict(m) for m in conn.execute(stmt).mappings().all()]

    def _process_row(self, row: dict[str, object], now: datetime) -> None:
        cron = row["schedule_cron"]
        last_fired = row["schedule_last_fired_at"]
        decision = evaluate_schedule(
            enabled=bool(row["enabled"]),
            schedule_enabled=bool(row["schedule_enabled"]),
            cron=cron if isinstance(cron, str) else None,
            now=now,
            last_fired=last_fired if isinstance(last_fired, datetime) else None,
            timezone=self._timezone,
        )
        if decision.action is ScheduleAction.SKIP or decision.fire_at is None:
            return

        workflow_id = str(row["id"])
        fire_at = decision.fire_at
        # 原子认领:守卫条件把锚点从 NULL / 更早值推进到 fire_at;rowcount==1 才算认领。
        # SEED 与 FIRE 都在此认领(SEED 只推进锚点不入队),同一事务里连带入队 job。
        run_id: str | None = None
        owner_id: str | None = None
        current_project_id: str | None = None
        with self._services.engine.begin() as conn:
            claimed = conn.execute(
                update(workflows)
                .where(
                    workflows.c.id == workflow_id,
                    workflows.c.enabled.is_(True),
                    workflows.c.schedule_enabled.is_(True),
                    or_(
                        workflows.c.schedule_last_fired_at.is_(None),
                        workflows.c.schedule_last_fired_at < fire_at,
                    ),
                )
                .values(schedule_last_fired_at=fire_at)
            ).rowcount
            if claimed != 1:
                # 并发认领 / 期间被禁用改动:让给对方,本 tick 不触发。
                return
            if decision.action is ScheduleAction.SEED:
                logger.info(
                    "workflow schedule baseline seeded",
                    workflow_id=workflow_id,
                    fire_at=fire_at.isoformat(),
                )
                return
            current = _locked_workflow_execution_row(conn, workflow_id)
            if current is None:
                return
            created_by = current["created_by"]
            if not isinstance(created_by, str) or not created_by:
                # 创建者被删(created_by SET NULL):无法归属 owner(jobs.owner NOT NULL)。
                # 锚点已推进 → 只告警一次(不逐 tick 刷屏),调度暂停至有人重存该 workflow。
                logger.warning(
                    "workflow schedule skipped: no owner",
                    workflow_id=workflow_id,
                )
                return
            owner_id = created_by
            current_project_id = str(current["project_id"])
            spec = WorkflowSpec.model_validate(current["dag_jsonb"] or {})
            job = build_workflow_run_job(
                workflow_id=workflow_id,
                workflow_name=str(current["name"]),
                project_id=current_project_id,
                owner_user_id=owner_id,
                spec=spec,
                trigger="schedule",
                # Freeze built-ins at the nominal UTC fire point, not at a
                # potentially delayed tick scan (ADR-0009 2.4.x addendum).
                now=fire_at,
            )
            _enqueue_in_txn(conn, job)
            run_id = job.id

        # 到此只可能是 FIRE 已成功入队(其余分支均在事务内 return)。
        assert current_project_id is not None
        self._services.write_audit(
            user_id=owner_id,
            project_id=current_project_id,
            action="workflow_run_trigger",
            resource_type="workflow",
            resource_id=workflow_id,
            result="accepted",
            request_id=None,
            ip=None,
            user_agent=None,
            detail={"run_id": run_id, "trigger": "schedule", "fire_at": fire_at.isoformat()},
        )
        logger.info(
            "workflow schedule fired",
            workflow_id=workflow_id,
            run_id=run_id,
            fire_at=fire_at.isoformat(),
        )

    # ── C-10 sensor 检查派发 ────────────────────────────────────────────────

    def _sensor_candidate_rows(self) -> list[dict[str, object]]:
        stmt = select(
            workflows.c.id,
            workflows.c.project_id,
            workflows.c.name,
            workflows.c.enabled,
            workflows.c.created_by,
            workflows.c.dag_jsonb,
            workflows.c.sensor_enabled,
            workflows.c.sensor_last_checked_at,
            workflows.c.sensor_last_triggered_at,
        ).where(
            workflows.c.enabled.is_(True),
            workflows.c.sensor_enabled.is_(True),
        )
        with self._services.engine.connect() as conn:
            return [dict(m) for m in conn.execute(stmt).mappings().all()]

    def _process_sensor_row(self, row: dict[str, object], now: datetime) -> None:
        workflow_id = str(row["id"])
        spec = WorkflowSpec.model_validate(row["dag_jsonb"] or {})
        sensor = spec.sensor
        if sensor is None or not sensor.enabled:
            # 冗余列说 sensor 开,但 dag_jsonb 无 sensor / 已禁用(列一时未同步):不派。
            return
        last_checked = row["sensor_last_checked_at"]
        last_triggered = row["sensor_last_triggered_at"]
        decision = evaluate_sensor(
            enabled=bool(row["enabled"]),
            sensor_enabled=bool(row["sensor_enabled"]),
            now=now,
            last_checked=last_checked if isinstance(last_checked, datetime) else None,
            check_interval_seconds=sensor.check_interval_seconds,
            last_triggered=last_triggered if isinstance(last_triggered, datetime) else None,
            cooldown_seconds=sensor.cooldown_seconds,
        )
        if decision.action is SensorAction.SKIP:
            return

        prev_checked = last_checked if isinstance(last_checked, datetime) else None
        # 原子认领 sensor_last_checked_at(compare-and-swap 读到的旧值):单 API 实例本
        # 无竞争,此守卫顺带兜住 HA 多实例误重复(HA 去重仍属后置,ADR-0009 Non-Goals)。
        with self._services.engine.begin() as conn:
            claim = update(workflows).where(
                workflows.c.id == workflow_id,
                workflows.c.enabled.is_(True),
                workflows.c.sensor_enabled.is_(True),
                (
                    workflows.c.sensor_last_checked_at.is_(None)
                    if prev_checked is None
                    else workflows.c.sensor_last_checked_at == prev_checked
                ),
            )
            claimed = conn.execute(claim.values(sensor_last_checked_at=now)).rowcount
            if claimed != 1:
                return
            current = _locked_workflow_execution_row(conn, workflow_id)
            if current is None:
                return
            spec = WorkflowSpec.model_validate(current["dag_jsonb"] or {})
            sensor = spec.sensor
            if sensor is None or not sensor.enabled:
                return
            created_by = current["created_by"]
            if not isinstance(created_by, str) or not created_by:
                # 创建者被删(created_by SET NULL):无法归属 owner(jobs.owner NOT NULL)。
                # 锚点已推进 → 只告警一次(不逐 tick 刷屏),sensor 暂停至有人重存该 workflow。
                logger.warning(
                    "workflow sensor check skipped: no owner",
                    workflow_id=workflow_id,
                )
                return
            job = build_sensor_check_job(
                workflow_id=workflow_id,
                workflow_name=str(current["name"]),
                project_id=str(current["project_id"]),
                owner_user_id=created_by,
                datasource_id=sensor.datasource_id,
                sensor_sql=sensor.sql,
                cooldown_seconds=sensor.cooldown_seconds,
                spec=spec,
                now=now,
            )
            _enqueue_in_txn(conn, job)
        logger.info(
            "workflow sensor check dispatched",
            workflow_id=workflow_id,
            check_job_id=job.id,
        )
