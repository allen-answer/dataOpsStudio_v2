"""C-10 sensor 检查 job 的 worker 侧单测:真值判定 / 冷却期守卫 / guard 拦写 /
禁用不触发。SQL 在 worker 连库执行(此处用 fake adapter),条件真经
SensorTriggerCatalog 带冷却期守卫入队 workflow_run。"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import pytest

from app.dbclients.sql_guard import SqlGuardError
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.schema import Row
from app.domain.workflow import WorkflowSpec
from app.worker import (
    WorkerRunner,
    WorkerRunnerConfig,
    _sensor_cell_truthy,
    _sensor_first_cell,
)
from tests.unit.test_worker import (  # reuse shared fakes
    _adapter_factory,
    _conn_info,
    _FakeAdapter,
    _FakeBackend,
    _FakeResultStore,
)

_SPEC = {
    "nodes": [
        {
            "id": "extract",
            "job_kind": "sql_query",
            "payload": {"sql": "SELECT 1"},
            "timeout_seconds": 60,
        }
    ],
    "edges": [],
    "sensor": {
        "sql": "SELECT COUNT(*) FROM arrivals",
        "datasource_id": "ds_1",
        "check_interval_seconds": 60,
        "cooldown_seconds": 300,
    },
}


class _FakeSensorCatalog:
    def __init__(self, *, claim_ok: bool = True) -> None:
        self._claim_ok = claim_ok
        self.calls: list[dict[str, Any]] = []

    def claim_trigger_and_enqueue(
        self,
        *,
        workflow_id: str,
        now: Any,
        cooldown_seconds: int,
        run_job: Job,
    ) -> bool:
        self.calls.append(
            {
                "workflow_id": workflow_id,
                "cooldown_seconds": cooldown_seconds,
                "run_job": run_job,
            }
        )
        return self._claim_ok


def _sensor_job(sensor_sql: str = "SELECT COUNT(*) FROM arrivals") -> Job:
    return Job(
        id="sensor-job-1",
        kind=JobKind.WORKFLOW_SENSOR_CHECK,
        status=JobStatus.PENDING,
        owner_user_id="u_1",
        project_id="p_1",
        datasource_ids=["ds_1"],
        priority=-1,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id="audit-1",
        payload={
            "workflow_id": "wf-1",
            "workflow_name": "daily",
            "datasource_id": "ds_1",
            "sensor_sql": sensor_sql,
            "cooldown_seconds": 300,
            "spec": WorkflowSpec.model_validate(_SPEC).model_dump(mode="json"),
        },
    )


def _runner(
    backend: _FakeBackend,
    adapter: _FakeAdapter,
    catalog: _FakeSensorCatalog | None,
) -> WorkerRunner:
    return WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1"),
        sensor_trigger_catalog=catalog,
    )


# ── 真值判定辅助 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, True),
        (0, False),
        (5, True),
        (Decimal("0"), False),
        (Decimal("3"), True),
        (True, True),
        (False, False),
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("y", True),
        ("anything", True),
    ],
)
def test_sensor_cell_truthy(value: object, expected: bool) -> None:
    assert _sensor_cell_truthy(value) is expected


def test_sensor_first_cell_no_rows_is_falsy() -> None:
    cell = _sensor_first_cell(iter([]))
    assert _sensor_cell_truthy(cell) is False


def test_sensor_first_cell_takes_first_column_of_first_row() -> None:
    def _gen() -> Iterable[Row]:
        yield Row(values=[7, "ignored"])
        yield Row(values=[99])

    assert _sensor_first_cell(_gen()) == 7


# ── worker 执行:真则触发,假则不触发 ────────────────────────────────────────


def test_sensor_check_triggers_when_condition_true() -> None:
    backend = _FakeBackend([_sensor_job()])
    adapter = _FakeAdapter([Row(values=[3])])  # COUNT(*) = 3 → truthy
    catalog = _FakeSensorCatalog(claim_ok=True)
    runner = _runner(backend, adapter, catalog)

    assert runner.run_once() is True

    assert backend.failed == []
    assert len(catalog.calls) == 1
    run_job = catalog.calls[0]["run_job"]
    assert run_job.kind is JobKind.WORKFLOW_RUN
    assert run_job.payload["trigger"] == "sensor"
    assert run_job.payload["workflow_id"] == "wf-1"
    assert catalog.calls[0]["cooldown_seconds"] == 300
    # sensor 检查 job 本身成功终态(triggered=True 记入 result metadata)
    assert backend.completed[0][0] == "sensor-job-1"
    assert backend.completed[0][1].metadata["triggered"] is True


def test_sensor_check_does_not_trigger_when_condition_false() -> None:
    backend = _FakeBackend([_sensor_job()])
    adapter = _FakeAdapter([Row(values=[0])])  # COUNT(*) = 0 → falsy
    catalog = _FakeSensorCatalog(claim_ok=True)
    runner = _runner(backend, adapter, catalog)

    assert runner.run_once() is True

    assert catalog.calls == []  # 条件假:不入队 workflow_run
    assert backend.completed[0][1].metadata["triggered"] is False


def test_sensor_check_no_rows_does_not_trigger() -> None:
    backend = _FakeBackend([_sensor_job("SELECT 1 WHERE 1=0")])
    adapter = _FakeAdapter([])  # 无行 → falsy
    catalog = _FakeSensorCatalog(claim_ok=True)
    runner = _runner(backend, adapter, catalog)

    assert runner.run_once() is True

    assert catalog.calls == []


def test_sensor_check_in_cooldown_does_not_report_run_id() -> None:
    # 条件真但冷却期守卫未命中(catalog 返回 False):不算触发,run_id 为 None
    backend = _FakeBackend([_sensor_job()])
    adapter = _FakeAdapter([Row(values=[5])])
    catalog = _FakeSensorCatalog(claim_ok=False)
    runner = _runner(backend, adapter, catalog)

    assert runner.run_once() is True

    assert len(catalog.calls) == 1  # 尝试认领
    assert backend.completed[0][1].metadata["run_id"] is None
    assert backend.completed[0][1].metadata["triggered"] is True


def test_sensor_check_rejects_write_sql() -> None:
    # 纵深防御:worker 执行前 sql_guard 拦写语句(路由层已 400,这里兜底 fail)
    backend = _FakeBackend([_sensor_job("DELETE FROM arrivals")])
    adapter = _FakeAdapter([Row(values=[1])])
    catalog = _FakeSensorCatalog(claim_ok=True)
    runner = _runner(backend, adapter, catalog)

    assert runner.run_once() is True

    assert catalog.calls == []
    assert adapter.execute_select_calls == 0  # 写语句在跑 SQL 前就被拦下
    assert backend.completed == []
    assert backend.failed and backend.failed[0][0] == "sensor-job-1"


def test_sensor_guard_directly_rejects_write() -> None:
    from app.dbclients.sql_guard import validate_readonly_sql

    with pytest.raises(SqlGuardError):
        validate_readonly_sql("UPDATE arrivals SET x = 1")
