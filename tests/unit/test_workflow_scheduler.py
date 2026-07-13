"""Workflow cron tick 纯逻辑单测(PR-4b):cron 解析 / 到期判定 / 防重 / 播种 /
禁用不触发 / no-backfill / 共享入队构造。DB 认领与端到端见 integration PG 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import app.services.workflow_scheduler as scheduler_module
from app.api.app import _build_scheduler
from app.config import SchedulerConfig, Settings
from app.domain.workflow import CronSchedule, WorkflowSpec
from app.services.workflow_scheduler import (
    ScheduleAction,
    ScheduleDecision,
    SensorAction,
    WorkflowScheduler,
    build_sensor_check_job,
    build_workflow_run_job,
    evaluate_schedule,
    evaluate_sensor,
    most_recent_fire,
)

if TYPE_CHECKING:
    from app.api.services import ApiServices


def _spec(**overrides: object) -> WorkflowSpec:
    payload: dict[str, object] = {
        "nodes": [
            {
                "id": "extract",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT 1"},
                "timeout_seconds": 60,
            }
        ],
        "edges": [],
    }
    payload.update(overrides)
    return WorkflowSpec.model_validate(payload)


# ── most_recent_fire ─────────────────────────────────────────────────────────


def test_most_recent_fire_returns_prior_boundary() -> None:
    now = datetime(2026, 7, 8, 10, 7, 30, tzinfo=UTC)
    assert most_recent_fire("*/15 * * * *", now) == datetime(2026, 7, 8, 10, 0, tzinfo=UTC)


def test_most_recent_fire_daily_cron() -> None:
    now = datetime(2026, 7, 8, 10, 7, 0, tzinfo=UTC)
    # 每天 03:00 → 最近触发点是今天 03:00
    assert most_recent_fire("0 3 * * *", now) == datetime(2026, 7, 8, 3, 0, tzinfo=UTC)


def test_most_recent_fire_daily_cron_in_configured_timezone_returns_utc() -> None:
    now = datetime(2026, 7, 8, 0, 7, tzinfo=UTC)

    fire_at = most_recent_fire("0 3 * * *", now, timezone=ZoneInfo("Asia/Shanghai"))

    assert fire_at == datetime(2026, 7, 7, 19, 0, tzinfo=UTC)
    assert fire_at is not None
    assert fire_at.tzinfo is UTC


def test_most_recent_fire_unparseable_returns_none() -> None:
    now = datetime(2026, 7, 8, 10, 7, 0, tzinfo=UTC)
    assert most_recent_fire("99 * * * *", now) is None


# ── evaluate_schedule:到期 / 防重 / 播种 / 禁用 ──────────────────────────────

_NOW = datetime(2026, 7, 8, 10, 7, 30, tzinfo=UTC)
_PREV = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)  # */15 相对 _NOW 的最近点


def test_evaluate_seed_on_first_sight() -> None:
    d = evaluate_schedule(
        enabled=True, schedule_enabled=True, cron="*/15 * * * *", now=_NOW, last_fired=None
    )
    assert d.action is ScheduleAction.SEED
    assert d.fire_at == _PREV


def test_evaluate_fire_on_new_point() -> None:
    older = datetime(2026, 7, 8, 9, 45, tzinfo=UTC)
    d = evaluate_schedule(
        enabled=True, schedule_enabled=True, cron="*/15 * * * *", now=_NOW, last_fired=older
    )
    assert d.action is ScheduleAction.FIRE
    assert d.fire_at == _PREV


def test_evaluate_dedup_same_point() -> None:
    # last_fired 已等于当前最近触发点 → 不重复(防重核心)
    d = evaluate_schedule(
        enabled=True, schedule_enabled=True, cron="*/15 * * * *", now=_NOW, last_fired=_PREV
    )
    assert d.action is ScheduleAction.SKIP


def test_evaluate_dedup_future_last_fired() -> None:
    future = datetime(2026, 7, 8, 10, 15, tzinfo=UTC)
    d = evaluate_schedule(
        enabled=True, schedule_enabled=True, cron="*/15 * * * *", now=_NOW, last_fired=future
    )
    assert d.action is ScheduleAction.SKIP


def test_evaluate_non_utc_schedule_seeds_with_utc_fire_point() -> None:
    decision = evaluate_schedule(
        enabled=True,
        schedule_enabled=True,
        cron="0 3 * * *",
        now=datetime(2026, 7, 8, 0, 7, tzinfo=UTC),
        last_fired=None,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert decision.action is ScheduleAction.SEED
    assert decision.fire_at == datetime(2026, 7, 7, 19, 0, tzinfo=UTC)


def test_evaluate_non_utc_schedule_fires_at_new_utc_point() -> None:
    decision = evaluate_schedule(
        enabled=True,
        schedule_enabled=True,
        cron="0 3 * * *",
        now=datetime(2026, 7, 8, 0, 7, tzinfo=UTC),
        last_fired=datetime(2026, 7, 6, 19, 0, tzinfo=UTC),
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert decision.action is ScheduleAction.FIRE
    assert decision.fire_at == datetime(2026, 7, 7, 19, 0, tzinfo=UTC)


def test_evaluate_non_utc_schedule_deduplicates_utc_fire_point() -> None:
    decision = evaluate_schedule(
        enabled=True,
        schedule_enabled=True,
        cron="0 3 * * *",
        now=datetime(2026, 7, 8, 0, 7, tzinfo=UTC),
        last_fired=datetime(2026, 7, 7, 19, 0, tzinfo=UTC),
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    assert decision.action is ScheduleAction.SKIP


def test_evaluate_naive_last_fired_treated_as_utc() -> None:
    # DB 取回的 naive datetime 也按 UTC 比较,不炸(tz-naive/aware 混比)
    d = evaluate_schedule(
        enabled=True,
        schedule_enabled=True,
        cron="*/15 * * * *",
        now=_NOW,
        last_fired=_PREV.replace(tzinfo=None),
    )
    assert d.action is ScheduleAction.SKIP


def test_evaluate_no_backfill_single_fire_for_missed_window() -> None:
    # 停机很久错过多个触发点:只补最近的一个,不逐点回灌(ADR-0009 §1)
    long_ago = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 8, 10, 7, tzinfo=UTC)
    d = evaluate_schedule(
        enabled=True, schedule_enabled=True, cron="0 * * * *", now=now, last_fired=long_ago
    )
    assert d.action is ScheduleAction.FIRE
    assert d.fire_at == datetime(2026, 7, 8, 10, 0, tzinfo=UTC)  # 仅最近整点,非 7/1 起逐点


@pytest.mark.parametrize(
    ("enabled", "schedule_enabled", "cron"),
    [
        (False, True, "*/15 * * * *"),  # workflow 总开关关
        (True, False, "*/15 * * * *"),  # 调度开关关(禁用 schedule 不触发)
        (True, True, None),  # 无 cron
    ],
)
def test_evaluate_disabled_never_fires(
    enabled: bool, schedule_enabled: bool, cron: str | None
) -> None:
    d = evaluate_schedule(
        enabled=enabled,
        schedule_enabled=schedule_enabled,
        cron=cron,
        now=_NOW,
        last_fired=None,
    )
    assert d.action is ScheduleAction.SKIP


# ── build_workflow_run_job:手动/cron 共享入队构造 ──────────────────────────


def test_build_workflow_run_job_shape() -> None:
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    spec = _spec(variables={"region": "cn"})
    job = build_workflow_run_job(
        workflow_id="wf-1",
        workflow_name="daily",
        project_id="proj-1",
        owner_user_id="user-1",
        spec=spec,
        trigger="schedule",
        now=now,
    )
    assert job.kind.value == "workflow_run"
    assert job.priority == -1  # 与手动触发一致,低于交互 sql_query
    assert job.owner_user_id == "user-1"
    assert job.timeout_seconds == 60 + 600  # sum(node timeout) + 600
    assert job.payload["trigger"] == "schedule"
    assert job.payload["workflow_id"] == "wf-1"
    variables = job.payload["when_variables"]
    assert isinstance(variables, dict)
    assert variables["region"] == "cn"  # spec.variables 进入快照
    assert set(variables) >= {"today", "now", "year", "month", "day", "region"}  # builtin + spec


def test_build_workflow_run_job_variable_precedence() -> None:
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    spec = _spec(variables={"region": "spec"})
    job = build_workflow_run_job(
        workflow_id="wf-1",
        workflow_name="daily",
        project_id="proj-1",
        owner_user_id="user-1",
        spec=spec,
        trigger="manual",
        now=now,
        extra_variables={"region": "trigger"},
    )
    variables = job.payload["when_variables"]
    assert isinstance(variables, dict)
    assert variables["region"] == "trigger"  # 触发时 > spec 默认


# ── 域层 cron 语义校验(croniter 落地精确解析)──────────────────────────────


def test_cron_schedule_accepts_valid_cron() -> None:
    assert CronSchedule(cron="0 6 * * 1-5").cron == "0 6 * * 1-5"


def test_cron_schedule_rejects_out_of_range() -> None:
    # 字符集过关但字段越界 → croniter 语义关拦下
    with pytest.raises(ValidationError, match="invalid_cron"):
        CronSchedule(cron="99 * * * *")


def test_scheduler_cron_path_uses_process_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_timezone = ZoneInfo("Asia/Shanghai")
    captured_timezone = ZoneInfo("UTC")

    def fake_evaluate_schedule(
        *,
        enabled: bool,
        schedule_enabled: bool,
        cron: str | None,
        now: datetime,
        last_fired: datetime | None,
        timezone: ZoneInfo,
    ) -> ScheduleDecision:
        del enabled, schedule_enabled, cron, now, last_fired
        nonlocal captured_timezone
        captured_timezone = timezone
        return ScheduleDecision(ScheduleAction.SKIP, None)

    monkeypatch.setattr(scheduler_module, "evaluate_schedule", fake_evaluate_schedule)
    scheduler = WorkflowScheduler(
        cast("ApiServices", object()),
        tick_interval_seconds=30.0,
        timezone=configured_timezone,
    )

    scheduler._process_row(
        {
            "id": "wf-1",
            "enabled": True,
            "schedule_enabled": True,
            "schedule_cron": "0 3 * * *",
            "schedule_last_fired_at": None,
        },
        datetime(2026, 7, 8, 0, 7, tzinfo=UTC),
    )

    assert captured_timezone is configured_timezone


def test_build_scheduler_passes_configured_timezone() -> None:
    settings = Settings(
        scheduler=SchedulerConfig.model_validate({"timezone": "Asia/Shanghai"}),
    )

    scheduler = _build_scheduler(cast("ApiServices", object()), settings)

    assert scheduler is not None
    assert scheduler._timezone is settings.scheduler.timezone


# ── C-10 evaluate_sensor:检查间隔节流 / 冷却期 / 禁用 ────────────────────────

_SENSOR_NOW = datetime(2026, 7, 8, 10, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("enabled", "sensor_enabled"),
    [(False, True), (True, False)],
)
def test_evaluate_sensor_disabled_never_checks(enabled: bool, sensor_enabled: bool) -> None:
    d = evaluate_sensor(
        enabled=enabled,
        sensor_enabled=sensor_enabled,
        now=_SENSOR_NOW,
        last_checked=None,
        check_interval_seconds=60,
        last_triggered=None,
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.SKIP


def test_evaluate_sensor_first_sight_checks() -> None:
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=None,
        check_interval_seconds=60,
        last_triggered=None,
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.CHECK


def test_evaluate_sensor_throttles_within_interval() -> None:
    # 距上次检查 30s < 间隔 60s → 不派
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=_SENSOR_NOW - timedelta(seconds=30),
        check_interval_seconds=60,
        last_triggered=None,
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.SKIP


def test_evaluate_sensor_checks_after_interval() -> None:
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=_SENSOR_NOW - timedelta(seconds=90),
        check_interval_seconds=60,
        last_triggered=None,
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.CHECK


def test_evaluate_sensor_skips_within_cooldown() -> None:
    # 间隔已到,但距上次触发 100s < 冷却 300s → 不派(省无谓 SQL)
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=_SENSOR_NOW - timedelta(seconds=90),
        check_interval_seconds=60,
        last_triggered=_SENSOR_NOW - timedelta(seconds=100),
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.SKIP


def test_evaluate_sensor_checks_after_cooldown() -> None:
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=_SENSOR_NOW - timedelta(seconds=90),
        check_interval_seconds=60,
        last_triggered=_SENSOR_NOW - timedelta(seconds=400),
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.CHECK


def test_evaluate_sensor_zero_cooldown_ignores_last_triggered() -> None:
    # 冷却 0:只要检查间隔到就再派(条件持续为真可重复触发)
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=_SENSOR_NOW - timedelta(seconds=90),
        check_interval_seconds=60,
        last_triggered=_SENSOR_NOW - timedelta(seconds=1),
        cooldown_seconds=0,
    )
    assert d.action is SensorAction.CHECK


def test_evaluate_sensor_naive_anchors_treated_as_utc() -> None:
    # DB 取回的 naive datetime 也按 UTC 比较,不炸
    d = evaluate_sensor(
        enabled=True,
        sensor_enabled=True,
        now=_SENSOR_NOW,
        last_checked=(_SENSOR_NOW - timedelta(seconds=30)).replace(tzinfo=None),
        check_interval_seconds=60,
        last_triggered=None,
        cooldown_seconds=300,
    )
    assert d.action is SensorAction.SKIP


def test_build_sensor_check_job_shape() -> None:
    spec = _spec(
        sensor={
            "sql": "SELECT COUNT(*) FROM arrivals WHERE arrived_at > '2026-07-08'",
            "datasource_id": "ds-1",
            "check_interval_seconds": 60,
            "cooldown_seconds": 300,
        }
    )
    job = build_sensor_check_job(
        workflow_id="wf-1",
        workflow_name="daily",
        project_id="proj-1",
        owner_user_id="user-1",
        datasource_id="ds-1",
        sensor_sql="SELECT 1",
        cooldown_seconds=300,
        spec=spec,
        now=_SENSOR_NOW,
    )
    assert job.kind.value == "workflow_sensor_check"
    assert job.priority == -1
    assert job.owner_user_id == "user-1"
    assert job.datasource_ids == ["ds-1"]
    assert job.payload["workflow_id"] == "wf-1"
    assert job.payload["sensor_sql"] == "SELECT 1"
    assert job.payload["cooldown_seconds"] == 300
    assert job.payload["spec"]["sensor"]["datasource_id"] == "ds-1"
