"""Env-gated compare_run correctness test against real MySQL + DM.

The test uses only DATAOPS_TEST_* environment variables and never prints connection details.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.compare_result import decode_compare_result_row
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.protocol import SecretStore
from app.worker import WorkerRunner, WorkerRunnerConfig

pytestmark = pytest.mark.integration

_MYSQL_TABLE = "dosv2_compare_run_src"
_DM_TABLE = "DOSV2_COMPARE_RUN_TGT"
_MYSQL_SAMPLE_TABLE = "dosv2_compare_run_sample_src"
_DM_SAMPLE_TABLE = "DOSV2_COMPARE_RUN_SAMPLE_TGT"


def test_compare_run_four_buckets_match_real_mysql_and_dm(tmp_path: Any) -> None:
    mysql_env = _mysql_env_or_skip()
    dm_env = _dm_env_or_skip()
    result_store = LocalFsResultStore(tmp_path)
    job = _compare_job()
    sample_job = _sample_compare_job()
    backend = _FakeBackend([job])

    with (
        _seed_mysql_source(mysql_env),
        _seed_dm_target(dm_env),
        _seed_mysql_sample(mysql_env),
        _seed_dm_sample(dm_env),
    ):
        runner = WorkerRunner(
            backend,
            result_store,
            lambda datasource_id: _conn_info(datasource_id, mysql_env, dm_env),
            _adapter_factory(mysql_env, dm_env),
            WorkerRunnerConfig(worker_id="worker-1"),
        )

        assert runner.run_once() is True

        sample_backend = _FakeBackend([sample_job])
        sample_runner = WorkerRunner(
            sample_backend,
            result_store,
            lambda datasource_id: _conn_info(datasource_id, mysql_env, dm_env),
            _adapter_factory(mysql_env, dm_env),
            WorkerRunnerConfig(worker_id="worker-1"),
        )
        assert sample_runner.run_once() is True
        sample_metadata = sample_backend.completed[0][1].metadata

    assert backend.failed == []
    assert len(backend.completed) == 1
    metadata = backend.completed[0][1].metadata
    assert metadata["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 1,
    }
    diff_profile = metadata["diff_profile"]
    assert diff_profile["columns"]["amount"]["numeric_delta"]["constant_offset"] == "-1.00"
    assert diff_profile["columns"]["created_at"]["time_delta"]["constant_offset_seconds"] == -28800
    assert diff_profile["columns"]["created_at"]["time_delta"]["likely_timezone_offset"] is True
    assert result_store.fetch_range("rs-same", 0, 10) == []
    only_source = decode_compare_result_row(result_store.fetch_range("rs-only-source", 0, 1)[0])
    only_target = decode_compare_result_row(result_store.fetch_range("rs-only-target", 0, 1)[0])
    diff = decode_compare_result_row(result_store.fetch_range("rs-diff", 0, 1)[0])
    assert only_source["pk"] == {"id": 2}
    assert only_target["pk"] == {"id": 4}
    assert diff["pk"] == {"id": 3}
    assert {cell["column"] for cell in diff["cells"]} == {"name", "amount", "created_at"}

    sample_result = sample_metadata["sample_result"]
    assert sample_result["requested_rows"] == 3
    assert sample_result["sampled_rows"] == 3
    assert sample_result["all_sampled_equal"] is True
    assert sample_result["difference_rate_upper_bound"] is not None


def _compare_job() -> Job:
    return Job(
        id="job-compare",
        kind=JobKind.COMPARE_RUN,
        status=JobStatus.PENDING,
        owner_user_id="user-1",
        project_id="project-1",
        datasource_ids=["mysql", "dm"],
        priority=0,
        timeout_seconds=3600,
        resource_profile=ResourceProfile(timeout_seconds=3600),
        audit_id="audit-1",
        payload={
            "run_id": "run-compare",
            "task_id": "task-compare",
            "source_id": "mysql",
            "target_id": "dm",
            "source_ref": {"kind": "table", "table_name": _MYSQL_TABLE},
            "target_ref": {"kind": "table", "table_name": _DM_TABLE},
            "columns": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
                {"name": "amount", "type": "decimal"},
                {"name": "created_at", "type": "datetime"},
            ],
            "compare_rules": {"key_columns": ["id"]},
            "run_limits": {
                "recursive_checksum": True,
                "bisection_threshold": 16_000,
                "persist_same_bucket": False,
            },
            "bucket_result_set_ids": {
                "only_source": "rs-only-source",
                "only_target": "rs-only-target",
                "diff": "rs-diff",
                "same": "rs-same",
            },
        },
    )


def _sample_compare_job() -> Job:
    return Job(
        id="job-compare-sample",
        kind=JobKind.COMPARE_RUN,
        status=JobStatus.PENDING,
        owner_user_id="user-1",
        project_id="project-1",
        datasource_ids=["mysql", "dm"],
        priority=0,
        timeout_seconds=3600,
        resource_profile=ResourceProfile(timeout_seconds=3600),
        audit_id="audit-1",
        payload={
            "run_id": "run-compare-sample",
            "task_id": "task-compare",
            "source_id": "mysql",
            "target_id": "dm",
            "source_ref": {"kind": "table", "table_name": _MYSQL_SAMPLE_TABLE},
            "target_ref": {"kind": "table", "table_name": _DM_SAMPLE_TABLE},
            "columns": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ],
            "compare_rules": {"key_columns": ["id"]},
            "run_limits": {
                "sample_quick_check": True,
                "sample_size": 3,
                "sample_confidence": 0.95,
                "persist_same_bucket": False,
            },
            "bucket_result_set_ids": {
                "only_source": "rs-sample-only-source",
                "only_target": "rs-sample-only-target",
                "diff": "rs-sample-diff",
                "same": "rs-sample-same",
            },
        },
    )


@contextmanager
def _seed_mysql_source(env: dict[str, str]) -> Iterator[None]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_TABLE}`")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_TABLE}` ("
            "`id` INT PRIMARY KEY, "
            "`name` VARCHAR(64), "
            "`amount` DECIMAL(12,2), "
            "`created_at` DATETIME"
            ")"
        )
        cursor.executemany(
            f"INSERT INTO `{_MYSQL_TABLE}` VALUES (%s, %s, %s, %s)",
            [
                (1, "same", "10.00", "2026-01-01 00:00:00"),
                (2, "left", "20.00", "2026-01-02 00:00:00"),
                (3, "old", "30.00", "2026-01-03 00:00:00"),
            ],
        )
        conn.commit()
        yield
    finally:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_TABLE}`")
            conn.commit()
        finally:
            cursor.close()
            conn.close()


@contextmanager
def _seed_dm_target(env: dict[str, str]) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_TABLE}"))
        cursor.execute(
            f"CREATE TABLE {_DM_TABLE} ("
            "ID INT PRIMARY KEY, "
            "NAME VARCHAR(64), "
            "AMOUNT DECIMAL(12,2), "
            "CREATED_AT TIMESTAMP"
            ")"
        )
        cursor.executemany(
            f"INSERT INTO {_DM_TABLE} VALUES (?, ?, ?, ?)",
            [
                (1, "same", "10.00", "2026-01-01 00:00:00"),
                (3, "new", "31.00", "2026-01-03 08:00:00"),
                (4, "right", "40.00", "2026-01-04 00:00:00"),
            ],
        )
        conn.commit()
        yield
    finally:
        try:
            _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_TABLE}"))
            conn.commit()
        finally:
            cursor.close()
            conn.close()


@contextmanager
def _seed_mysql_sample(env: dict[str, str]) -> Iterator[None]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_SAMPLE_TABLE}`")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_SAMPLE_TABLE}` (`id` INT PRIMARY KEY, `name` VARCHAR(64))"
        )
        cursor.executemany(
            f"INSERT INTO `{_MYSQL_SAMPLE_TABLE}` VALUES (%s, %s)",
            [(1, "same-1"), (2, "same-2"), (3, "same-3")],
        )
        conn.commit()
        yield
    finally:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_SAMPLE_TABLE}`")
            conn.commit()
        finally:
            cursor.close()
            conn.close()


@contextmanager
def _seed_dm_sample(env: dict[str, str]) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_SAMPLE_TABLE}"))
        cursor.execute(f"CREATE TABLE {_DM_SAMPLE_TABLE} (ID INT PRIMARY KEY, NAME VARCHAR(64))")
        cursor.executemany(
            f"INSERT INTO {_DM_SAMPLE_TABLE} VALUES (?, ?)",
            [(1, "same-1"), (2, "same-2"), (3, "same-3")],
        )
        conn.commit()
        yield
    finally:
        try:
            _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_SAMPLE_TABLE}"))
            conn.commit()
        finally:
            cursor.close()
            conn.close()


def _adapter_factory(
    mysql_env: dict[str, str],
    dm_env: dict[str, str],
) -> Callable[[DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]], Any]:
    def factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> Any:
        del column_sink
        if conn_info.db_type is DbType.MYSQL:
            return MySQLAdapter(
                conn_info,
                cast(SecretStore, _EnvSecretStore(mysql_env["password"])),
                cancel_check=cancel_check,
            )
        return DMAdapter(
            conn_info,
            cast(SecretStore, _EnvSecretStore(dm_env["password"])),
            cancel_check=cancel_check,
        )

    return factory


def _conn_info(
    datasource_id: str,
    mysql_env: dict[str, str],
    dm_env: dict[str, str],
) -> DatasourceConnInfo:
    if datasource_id == "mysql":
        return DatasourceConnInfo(
            host=mysql_env["host"],
            port=int(mysql_env["port"]),
            username=mysql_env["user"],
            database=mysql_env["database"],
            password_ref=SecretRef(
                ref="env://mysql-compare-run",
                kind=SecretKind.DATASOURCE_PASSWORD,
            ),
            db_type=DbType.MYSQL,
        )
    return DatasourceConnInfo(
        host=dm_env["host"],
        port=int(dm_env["port"]),
        username=dm_env["user"],
        database=dm_env["database"],
        password_ref=SecretRef(ref="env://dm-compare-run", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
    )


def _mysql_env_or_skip() -> dict[str, str]:
    return _env_or_skip(
        {
            "host": "DATAOPS_TEST_MYSQL_HOST",
            "port": "DATAOPS_TEST_MYSQL_PORT",
            "user": "DATAOPS_TEST_MYSQL_USER",
            "password": "DATAOPS_TEST_MYSQL_PASSWORD",
            "database": "DATAOPS_TEST_MYSQL_DATABASE",
        },
        "MySQL",
    )


def _dm_env_or_skip() -> dict[str, str]:
    return _env_or_skip(
        {
            "host": "DATAOPS_TEST_DM_HOST",
            "port": "DATAOPS_TEST_DM_PORT",
            "user": "DATAOPS_TEST_DM_USERNAME",
            "password": "DATAOPS_TEST_DM_PASSWORD",
            "database": "DATAOPS_TEST_DM_DATABASE",
        },
        "DM",
    )


def _env_or_skip(mapping: dict[str, str], label: str) -> dict[str, str]:
    values = {name: os.environ.get(env_name) for name, env_name in mapping.items()}
    missing = [mapping[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"{label} integration env not set: {', '.join(missing)}")
    return {name: str(value) for name, value in values.items()}


def _connect_mysql(pymysql: Any, env: dict[str, str]) -> Any:
    try:
        return pymysql.connect(
            host=env["host"],
            port=int(env["port"]),
            user=env["user"],
            password=env["password"],
            database=env["database"],
            charset="utf8mb4",
            connect_timeout=10,
        )
    except Exception as exc:
        raise RuntimeError(
            f"MySQL compare_run connection failed: {type(exc).__name__}; details redacted"
        ) from exc


def _connect_dm(dm: Any, env: dict[str, str]) -> Any:
    try:
        return dm.connect(
            user=env["user"],
            password=env["password"],
            server=env["host"],
            port=int(env["port"]),
            schema=env["database"],
        )
    except Exception as exc:
        raise RuntimeError(
            f"DM compare_run connection failed: {type(exc).__name__}; details redacted"
        ) from exc


def _ignore_error(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        return


class _FakeBackend:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.completed: list[tuple[str, ResultRef]] = []
        self.failed: list[tuple[str, str]] = []

    def claim_next(self, worker_id: str) -> Job | None:
        if not self._jobs:
            return None
        return self._jobs.pop(0).model_copy(
            update={"status": JobStatus.RUNNING, "worker_id": worker_id}
        )

    def complete(self, job_id: str, result_ref: ResultRef) -> None:
        self.completed.append((job_id, result_ref))

    def fail(self, job_id: str, error: str) -> None:
        self.failed.append((job_id, error))

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        del job_id, worker_id

    def mark_cancelled(self, job_id: str, reason: str = "cancelled") -> None:
        del job_id, reason

    def is_cancel_requested(self, job_id: str) -> bool:
        del job_id
        return False

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 100,
    ) -> object:
        del heartbeat_timeout_seconds, limit
        return object()


class _EnvSecretStore:
    def __init__(self, password: str) -> None:
        self._password = password

    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return self._password

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        raise NotImplementedError

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        raise NotImplementedError

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        raise NotImplementedError

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        raise NotImplementedError
