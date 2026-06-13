from __future__ import annotations

import signal
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Protocol, cast

import structlog
from sqlalchemy import URL, create_engine, insert, select, update
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import datasources, jobs, result_sets
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind
from app.domain.plan import PlanNode
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.result_export import (
    ExportFormat,
    ExportSizeLimitExceeded,
    write_result_export,
)
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)


class JobCancelled(RuntimeError):
    """Job was cooperatively cancelled."""


class UnsupportedJobKindError(RuntimeError):
    """2.0.0 worker supports only the job kinds wired in this module."""


class OperationPolicyDeniedError(RuntimeError):
    """Datasource operation_policy denied this operation."""


class DatasourceConnectionTestError(RuntimeError):
    """Datasource test failed with a structured public test error code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class BackendLike(Protocol):
    def claim_next(self, worker_id: str) -> Job | None: ...

    def complete(self, job_id: str, result_ref: ResultRef) -> None: ...

    def fail(self, job_id: str, error: str) -> None: ...

    def heartbeat(self, job_id: str, worker_id: str) -> None: ...

    def mark_cancelled(self, job_id: str, reason: str = "cancelled") -> None: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 100,
    ) -> object: ...


class ResultStoreLike(Protocol):
    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]: ...

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None: ...

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef: ...

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None: ...

    def get_spool_manifest(self, result_set_id: str) -> dict[str, Any]: ...

    def spool_ref(self, result_set_id: str) -> ResultRef: ...

    def delete_spool(self, result_set_id: str) -> bool: ...

    def gc_expired(self) -> int: ...


class DatabaseAdapterLike(Protocol):
    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]: ...

    def explain(self, sql: str) -> PlanNode: ...

    def test_connection(self) -> bool: ...


class ResultSetCatalogLike(Protocol):
    def write_streaming(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        console_id: str | None,
    ) -> None: ...

    def write_complete(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        truncated: bool,
        console_id: str | None,
    ) -> None: ...

    def write_terminal(
        self,
        *,
        result_set_id: str,
        state: str,
    ) -> None: ...


class JobErrorCodeWriterLike(Protocol):
    def set_error_code(self, job_id: str, error_code: JobErrorCode) -> None: ...


DatasourceLoader = Callable[[str], DatasourceConnInfo]
AdapterFactory = Callable[
    [DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]],
    DatabaseAdapterLike,
]


@dataclass(frozen=True)
class WorkerRunnerConfig:
    worker_id: str
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0
    sql_spool_batch_size: int = 1000
    cancel_check_row_interval: int = 5000
    result_gc_interval_seconds: float = 600.0
    export_limit_mb: int = 1024


@dataclass(frozen=True)
class _ExecutionOutcome:
    result_ref: ResultRef
    loaded_rows: int | None = None


class WorkerRunner:
    def __init__(
        self,
        backend: BackendLike,
        result_store: ResultStoreLike,
        datasource_loader: DatasourceLoader,
        adapter_factory: AdapterFactory,
        config: WorkerRunnerConfig,
        result_set_catalog: ResultSetCatalogLike | None = None,
        job_error_code_writer: JobErrorCodeWriterLike | None = None,
    ) -> None:
        if config.sql_spool_batch_size <= 0:
            raise ValueError("sql_spool_batch_size must be positive")
        if config.cancel_check_row_interval <= 0:
            raise ValueError("cancel_check_row_interval must be positive")
        if config.result_gc_interval_seconds <= 0:
            raise ValueError("result_gc_interval_seconds must be positive")
        if config.export_limit_mb <= 0:
            raise ValueError("export_limit_mb must be positive")
        self._backend = backend
        self._result_store = result_store
        self._datasource_loader = datasource_loader
        self._adapter_factory = adapter_factory
        self._config = config
        self._result_set_catalog = result_set_catalog
        self._job_error_code_writer = job_error_code_writer
        self._stop_requested = False
        self._next_result_gc_at = 0.0

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_forever(self) -> None:
        while not self._stop_requested:
            did_work = self.run_once()
            if not did_work:
                time.sleep(self._config.poll_interval_seconds)

    def run_once(self) -> bool:
        self._gc_result_store_if_due()
        reap_report = self._backend.reap_stale_running_jobs(self._config.heartbeat_timeout_seconds)
        _log_reap_report(reap_report)
        job = self._backend.claim_next(self._config.worker_id)
        if job is None:
            return False
        logger.info(
            "worker job claimed",
            job_id=job.id,
            kind=job.kind.value,
            worker_id=self._config.worker_id,
        )
        self._execute_claimed_job(job)
        return True

    def _execute_claimed_job(self, job: Job) -> None:
        started_at = time.monotonic()
        logger.info(
            "worker job start",
            job_id=job.id,
            kind=job.kind.value,
            worker_id=self._config.worker_id,
        )
        try:
            self._check_cancel(job.id)
            if job.kind is JobKind.TEST_CONNECTION:
                outcome = self._execute_test_connection(job)
            elif job.kind is JobKind.SQL_QUERY:
                outcome = self._execute_sql_query(job)
            elif job.kind is JobKind.SQL_EXPLAIN:
                outcome = self._execute_sql_explain(job)
            elif job.kind is JobKind.RESULT_EXPORT:
                outcome = self._execute_result_export(job)
            else:
                raise UnsupportedJobKindError(f"Unsupported job kind: {job.kind.value}")
        except JobCancelled as exc:
            self._backend.mark_cancelled(job.id, str(exc))
            self._write_result_set_terminal(job, "closed")
            logger.info(
                "worker job cancelled",
                job_id=job.id,
                kind=job.kind.value,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        except Exception as exc:
            public_error = _public_error_message(exc, job.kind)
            error_code = _job_error_code(exc, job.kind)
            self._backend.fail(job.id, public_error)
            self._write_error_code(job.id, error_code)
            self._write_result_set_terminal(job, "failed")
            logger.exception(
                "worker job failed",
                job_id=job.id,
                kind=job.kind.value,
                error_code=error_code.value,
                error_type=type(exc).__name__,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        else:
            self._backend.complete(job.id, outcome.result_ref)
            logger.info(
                "worker job complete",
                job_id=job.id,
                kind=job.kind.value,
                loaded_rows=outcome.loaded_rows,
                elapsed_seconds=_elapsed_seconds(started_at),
            )

    def _execute_sql_query(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        sql = _required_payload_str(payload, "sql")
        params = _payload_params(payload)
        datasource_id = _payload_datasource_id(job)
        result_set_id = str(payload.get("result_set_id") or job.id)
        console_id = _payload_optional_str(payload, "console_id")
        datasource = self._datasource_loader(datasource_id)
        _require_operation_allowed(datasource.operation_policy, "select")
        columns: list[Column] | None = None
        self._write_result_set_streaming(job, result_set_id, [], 0, console_id)

        def capture_columns(emitted_columns: list[Column]) -> None:
            nonlocal columns
            assert columns is None, "adapter emitted result columns more than once"
            columns = _copy_columns(emitted_columns)
            self._result_store.set_spool_columns(result_set_id, columns)

        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            capture_columns,
        )

        batch: list[Row] = []
        rows_since_cancel_check = 0
        cancel_check_row_interval = self._config.cancel_check_row_interval
        last_heartbeat = time.monotonic()
        for row in adapter.execute_select(sql, params):
            rows_since_cancel_check += 1
            if rows_since_cancel_check >= cancel_check_row_interval:
                self._check_cancel(job.id)
                rows_since_cancel_check = 0
            batch.append(row)
            if len(batch) >= self._config.sql_spool_batch_size:
                self._flush_batch(job.id, result_set_id, batch)
                self._write_result_set_streaming(
                    job,
                    result_set_id,
                    columns or [],
                    _spool_loaded_rows(self._result_store, result_set_id),
                    console_id,
                )
                batch = []
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat, force=True)
            else:
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat)

        if batch:
            self._flush_batch(job.id, result_set_id, batch)
            self._write_result_set_streaming(
                job,
                result_set_id,
                columns or [],
                _spool_loaded_rows(self._result_store, result_set_id),
                console_id,
            )
        self._heartbeat(job.id)
        result_ref = self._result_store.spool_ref(result_set_id)
        self._write_result_set_catalog(job, result_set_id, result_ref, columns or [], console_id)
        manifest = self._result_store.get_spool_manifest(result_set_id)
        return _ExecutionOutcome(
            result_ref=result_ref,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
        )

    def _execute_sql_explain(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        sql = _required_payload_str(payload, "sql")
        datasource_id = _payload_datasource_id(job)
        result_set_id = str(payload.get("result_set_id") or job.id)
        console_id = _payload_optional_str(payload, "console_id")
        datasource = self._datasource_loader(datasource_id)
        _require_operation_allowed(datasource.operation_policy, "explain")
        self._write_result_set_streaming(job, result_set_id, [], 0, console_id)
        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )

        self._check_cancel(job.id)
        plan = adapter.explain(sql)
        columns, rows = _plan_result_rows(plan)
        self._result_store.set_spool_columns(result_set_id, columns)
        if rows:
            self._flush_batch(job.id, result_set_id, rows)
        self._write_result_set_streaming(
            job,
            result_set_id,
            columns,
            _spool_loaded_rows(self._result_store, result_set_id),
            console_id,
        )
        self._heartbeat(job.id)
        result_ref = self._result_store.spool_ref(result_set_id)
        self._write_result_set_catalog(job, result_set_id, result_ref, columns, console_id)
        manifest = self._result_store.get_spool_manifest(result_set_id)
        return _ExecutionOutcome(
            result_ref=result_ref,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
        )

    def _execute_test_connection(self, job: Job) -> _ExecutionOutcome:
        datasource_id = _payload_datasource_id(job)
        datasource = self._datasource_loader(datasource_id)
        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )
        started_at = time.monotonic()
        if not adapter.test_connection():
            error_summary = getattr(adapter, "last_connection_error", None)
            error_code = _datasource_test_error_code(error_summary)
            logger.warning(
                "datasource test_connection failed",
                job_id=job.id,
                datasource_id=datasource_id,
                error_code=error_code,
                error_summary=error_summary if isinstance(error_summary, str) else None,
            )
            raise DatasourceConnectionTestError(error_code)
        self._heartbeat(job.id)
        metadata: dict[str, object] = {"latency_ms": _elapsed_ms(started_at)}
        server_version = getattr(adapter, "last_server_version", None)
        if isinstance(server_version, str) and server_version:
            metadata["server_version"] = server_version
        return _ExecutionOutcome(
            ResultRef(
                backend="connection_test",
                uri=f"test_connection/{job.id}",
                metadata=metadata,
            )
        )

    def _execute_result_export(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        source_result_set_id = _required_payload_str(payload, "source_result_set_id")
        export_format = _payload_export_format(payload)
        filename = _required_payload_str(payload, "filename")
        table_name = _payload_optional_str(payload, "table_name") or "exported_result"
        db_type = _payload_optional_str(payload, "source_db_type") or "mysql"
        manifest = self._result_store.get_spool_manifest(source_result_set_id)
        columns = _columns_from_manifest(manifest)
        limit_bytes = self._config.export_limit_mb * 1024 * 1024
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024, mode="w+b") as stream:
            binary_stream = cast(BinaryIO, stream)
            exported_bytes = write_result_export(
                stream=binary_stream,
                export_format=export_format,
                columns=columns,
                rows=self._iter_spool_rows(job.id, source_result_set_id),
                table_name=table_name,
                db_type=db_type,
                limit_bytes=limit_bytes,
            )
            binary_stream.seek(0)
            result_ref = self._result_store.put_export_artifact(job.id, filename, binary_stream)
        self._heartbeat(job.id)
        result_ref = result_ref.model_copy(
            update={
                "metadata": {
                    "format": export_format,
                    "filename": filename,
                    "bytes": exported_bytes,
                    "source_result_set_id": source_result_set_id,
                }
            }
        )
        return _ExecutionOutcome(
            result_ref=result_ref,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
        )

    def _iter_spool_rows(self, job_id: str, result_set_id: str) -> Iterable[Row]:
        offset = 0
        last_heartbeat = time.monotonic()
        while True:
            self._check_cancel(job_id)
            batch = self._result_store.fetch_range(
                result_set_id,
                offset,
                self._config.sql_spool_batch_size,
            )
            if not batch:
                return
            yield from batch
            offset += len(batch)
            last_heartbeat = self._heartbeat_if_due(job_id, last_heartbeat, force=True)

    def _flush_batch(self, job_id: str, result_set_id: str, batch: list[Row]) -> None:
        # Cancel during append_spool is caught at the next batch or row interval.
        self._check_cancel(job_id)
        self._result_store.append_spool(result_set_id, batch)

    def _heartbeat_if_due(
        self,
        job_id: str,
        last_heartbeat: float,
        *,
        force: bool = False,
    ) -> float:
        now = time.monotonic()
        if force or now - last_heartbeat >= self._config.heartbeat_interval_seconds:
            self._heartbeat(job_id)
            return now
        return last_heartbeat

    def _heartbeat(self, job_id: str) -> None:
        self._backend.heartbeat(job_id, self._config.worker_id)
        logger.debug(
            "worker job heartbeat",
            job_id=job_id,
            worker_id=self._config.worker_id,
        )

    def _check_cancel(self, job_id: str) -> None:
        if self._backend.is_cancel_requested(job_id):
            raise JobCancelled("cancel requested")

    def _write_error_code(self, job_id: str, error_code: JobErrorCode) -> None:
        if self._job_error_code_writer is None:
            return
        self._job_error_code_writer.set_error_code(job_id, error_code)

    def _write_result_set_catalog(
        self,
        job: Job,
        result_set_id: str,
        result_ref: ResultRef,
        columns: list[Column],
        console_id: str | None,
    ) -> None:
        if self._result_set_catalog is None:
            return
        manifest = self._result_store.get_spool_manifest(result_set_id)
        self._result_set_catalog.write_complete(
            result_set_id=result_set_id,
            execution_id=job.id,
            storage_ref=result_ref,
            columns=columns,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
            truncated=_manifest_bool(manifest, "truncated"),
            console_id=console_id,
        )

    def _write_result_set_streaming(
        self,
        job: Job,
        result_set_id: str,
        columns: list[Column],
        loaded_rows: int,
        console_id: str | None,
    ) -> None:
        if self._result_set_catalog is None:
            return
        self._result_set_catalog.write_streaming(
            result_set_id=result_set_id,
            execution_id=job.id,
            storage_ref=self._result_store.spool_ref(result_set_id),
            columns=columns,
            loaded_rows=loaded_rows,
            console_id=console_id,
        )

    def _write_result_set_terminal(self, job: Job, state: str) -> None:
        if job.kind not in {JobKind.SQL_QUERY, JobKind.SQL_EXPLAIN}:
            return
        result_set_id = _payload_optional_str(job.payload, "result_set_id")
        if result_set_id is None:
            return
        if self._result_set_catalog is not None:
            self._result_set_catalog.write_terminal(
                result_set_id=result_set_id,
                state=state,
            )
        if state == "closed":
            self._delete_result_spool(result_set_id)

    def _delete_result_spool(self, result_set_id: str) -> None:
        started_at = time.monotonic()
        try:
            deleted = self._result_store.delete_spool(result_set_id)
        except Exception:
            logger.exception(
                "worker result spool delete failed",
                result_set_id=result_set_id,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
            return
        if deleted:
            logger.info(
                "worker result spool deleted",
                result_set_id=result_set_id,
                elapsed_seconds=_elapsed_seconds(started_at),
            )

    def _gc_result_store_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_result_gc_at:
            return
        started_at = now
        try:
            cleaned = self._result_store.gc_expired()
        except Exception:
            logger.exception(
                "worker result spool gc failed",
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        else:
            logger.info(
                "worker result spool gc complete",
                cleaned=cleaned,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        finally:
            self._next_result_gc_at = now + self._config.result_gc_interval_seconds


class PostgresDatasourceLoader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, datasource_id: str) -> DatasourceConnInfo:
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(datasources).where(datasources.c.id == datasource_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError(f"Datasource not found: {datasource_id}")

        database_name = row["database_name"]
        if database_name is None:
            raise ValueError(f"Datasource database_name is required: {datasource_id}")

        return DatasourceConnInfo(
            host=str(row["host"]),
            port=int(row["port"]),
            username=str(row["username"]),
            database=str(database_name),
            password_ref=SecretRef(
                ref=str(row["password_secret_ref"]),
                kind=SecretKind.DATASOURCE_PASSWORD,
            ),
            db_type=DbType(str(row["db_type"])),
            extra=dict(row["capability_profile"] or {}),
            operation_policy=OperationPolicy.model_validate(row["operation_policy"] or {}),
        )


class PostgresResultSetCatalog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write_streaming(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        console_id: str | None,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "execution_id": execution_id,
            "console_id": console_id,
            "storage_ref": storage_ref.model_dump(),
            "columns": [column.model_dump() for column in columns],
            "loaded_rows": loaded_rows,
            "total_rows": None,
            "state": "streaming",
            "updated_at": now,
        }
        with self._engine.begin() as conn:
            result = conn.execute(
                update(result_sets).where(result_sets.c.id == result_set_id).values(**values)
            )
            if result.rowcount == 0:
                conn.execute(
                    insert(result_sets).values(
                        id=result_set_id,
                        created_at=now,
                        **values,
                    )
                )

    def write_complete(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        truncated: bool,
        console_id: str | None,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "execution_id": execution_id,
            "console_id": console_id,
            "storage_ref": storage_ref.model_dump(),
            "columns": [column.model_dump() for column in columns],
            "loaded_rows": loaded_rows,
            "total_rows": None if truncated else loaded_rows,
            "state": "complete",
            "updated_at": now,
        }
        with self._engine.begin() as conn:
            result = conn.execute(
                update(result_sets).where(result_sets.c.id == result_set_id).values(**values)
            )
            if result.rowcount == 0:
                conn.execute(
                    insert(result_sets).values(
                        id=result_set_id,
                        created_at=now,
                        **values,
                    )
                )

    def write_terminal(self, *, result_set_id: str, state: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(result_sets)
                .where(result_sets.c.id == result_set_id)
                .values(state=state, updated_at=datetime.now(UTC))
            )


class PostgresJobErrorCodeWriter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def set_error_code(self, job_id: str, error_code: JobErrorCode) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.status == "failed")
                .values(error_code=error_code.value)
            )


def build_worker_runner(settings: Settings | None = None) -> WorkerRunner:
    actual_settings = settings or load_settings()
    configure_logging(actual_settings.logging.level)
    bootstrap = LocalFileBootstrapSecrets(
        master_key_file=actual_settings.bootstrap.master_key_file,
        pg_app_password_file=actual_settings.bootstrap.pg_app_password_file,
        pg_superuser_password_file=actual_settings.bootstrap.pg_superuser_password_file,
        license_file=actual_settings.bootstrap.license_file,
    )
    engine = _create_metadata_engine(actual_settings, bootstrap.get_pg_app_password())
    backend = PostgresJobBackend(engine, worker_id=actual_settings.worker.worker_id)
    secret_store = LocalFileSecretStore(engine, bootstrap)
    result_store = LocalFsResultStore(
        actual_settings.result_store.local_root,
        spool_max_rows=actual_settings.result_store.spool_max_rows,
        spool_max_bytes=actual_settings.result_store.spool_max_bytes,
        result_ttl_days=actual_settings.result_store.result_ttl_days,
        sql_export_ttl_hours=actual_settings.result_store.sql_export_ttl_hours,
    )
    datasource_loader = PostgresDatasourceLoader(engine)

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> DatabaseAdapterLike:
        return build_database_adapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=actual_settings.result_store.cursor_max_hold_seconds,
            statement_timeout_seconds=actual_settings.worker.heartbeat_timeout_seconds,
        )

    return WorkerRunner(
        backend,
        result_store,
        datasource_loader,
        adapter_factory,
        WorkerRunnerConfig(
            worker_id=actual_settings.worker.worker_id,
            heartbeat_interval_seconds=actual_settings.worker.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=actual_settings.worker.heartbeat_timeout_seconds,
            poll_interval_seconds=actual_settings.worker.poll_interval_seconds,
            cancel_check_row_interval=actual_settings.worker.cancel_check_row_interval,
            result_gc_interval_seconds=actual_settings.worker.result_gc_interval_seconds,
            export_limit_mb=actual_settings.result_store.export_limit_mb,
        ),
        PostgresResultSetCatalog(engine),
        PostgresJobErrorCodeWriter(engine),
    )


def main() -> None:
    runner = build_worker_runner()
    logger.info("worker starting")

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("worker stop requested", signal=signum)
        del frame
        runner.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    runner.run_forever()
    logger.info("worker stopped")


def _create_metadata_engine(settings: Settings, password: str) -> Engine:
    url = URL.create(
        "postgresql+psycopg",
        username=settings.db.user,
        password=password,
        host=settings.db.host,
        port=settings.db.port,
        database=settings.db.database,
    )
    return create_engine(
        url,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.max_overflow,
        pool_recycle=settings.db.pool_recycle_seconds,
        pool_pre_ping=settings.db.pool_pre_ping,
    )


def _required_payload_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Job payload requires non-empty string field: {key}")
    return value


def _payload_params(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("params", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Job payload params must be an object")
    return dict(value)


def _payload_datasource_id(job: Job) -> str:
    value = job.payload.get("datasource_id")
    if isinstance(value, str) and value:
        return value
    if job.datasource_ids:
        return job.datasource_ids[0]
    raise ValueError("Job payload requires datasource_id or job.datasource_ids")


def _payload_optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _payload_export_format(payload: dict[str, object]) -> ExportFormat:
    value = _required_payload_str(payload, "format")
    if value not in {"csv", "excel", "json", "sql"}:
        raise ValueError(f"Unsupported export format: {value}")
    return cast(ExportFormat, value)


def _spool_loaded_rows(result_store: ResultStoreLike, result_set_id: str) -> int:
    return _manifest_int(result_store.get_spool_manifest(result_set_id), "loaded_rows")


def _copy_columns(columns: list[Column]) -> list[Column]:
    return [Column.model_validate(column.model_dump()) for column in columns]


def _elapsed_seconds(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _log_reap_report(report: object) -> None:
    requeued = getattr(report, "requeued", 0)
    failed = getattr(report, "failed", 0)
    if not isinstance(requeued, int):
        requeued = 0
    if not isinstance(failed, int):
        failed = 0
    if requeued or failed:
        logger.info("worker reaped stale jobs", requeued=requeued, failed=failed)


def _manifest_int(manifest: dict[str, Any], key: str) -> int:
    value = manifest.get(key, 0)
    return value if isinstance(value, int) else 0


def _manifest_bool(manifest: dict[str, Any], key: str) -> bool:
    value = manifest.get(key, False)
    return value if isinstance(value, bool) else False


def _columns_from_manifest(manifest: dict[str, Any]) -> list[Column]:
    raw_columns = manifest.get("columns")
    if not isinstance(raw_columns, list):
        return []
    return [Column.model_validate(column) for column in raw_columns if isinstance(column, dict)]


def _plan_result_rows(plan: PlanNode) -> tuple[list[Column], list[Row]]:
    raw_rows = plan.details.get("rows")
    if isinstance(raw_rows, list):
        normalized_rows = [_normalize_plan_row(item) for item in raw_rows]
        if normalized_rows:
            column_names = _plan_column_names(normalized_rows)
            columns = [Column(name=name, type=ColumnType.UNKNOWN) for name in column_names]
            rows = [Row(values=[row.get(name) for name in column_names]) for row in normalized_rows]
            return columns, rows
    columns = [
        Column(name="operation", type=ColumnType.UNKNOWN),
        Column(name="details", type=ColumnType.UNKNOWN),
    ]
    return columns, [Row(values=[plan.operation, plan.details])]


def _normalize_plan_row(item: object) -> dict[str, object]:
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}
    return {"value": item}


def _plan_column_names(rows: list[dict[str, object]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                names.append(key)
    return names


def _require_operation_allowed(policy: OperationPolicy, operation: str) -> None:
    if operation == "select" and not policy.allow_select:
        raise OperationPolicyDeniedError("select denied by datasource operation_policy")
    if operation == "explain" and not policy.allow_explain:
        raise OperationPolicyDeniedError("explain denied by datasource operation_policy")


def _public_error_message(exc: Exception, kind: JobKind) -> str:
    if isinstance(exc, DatasourceConnectionTestError):
        return exc.error_code
    if isinstance(exc, OperationPolicyDeniedError):
        return "permission_denied"
    if isinstance(exc, UnsupportedDbTypeError):
        return "unsupported_db_type"
    if isinstance(exc, ExportSizeLimitExceeded):
        return "export_limit_exceeded"
    if isinstance(exc, UnsupportedJobKindError):
        return "internal"
    if _is_timeout_error(exc):
        return "timeout"
    if _is_cancel_error(exc):
        return "cancelled"
    return _job_error_code(exc, kind).value


def _job_error_code(exc: Exception, kind: JobKind) -> JobErrorCode:
    if isinstance(exc, OperationPolicyDeniedError):
        return JobErrorCode.PERMISSION_DENIED
    if isinstance(exc, UnsupportedDbTypeError):
        return JobErrorCode.UNSUPPORTED_DB_TYPE
    if isinstance(exc, ExportSizeLimitExceeded):
        return JobErrorCode.EXPORT_LIMIT_EXCEEDED
    if _is_timeout_error(exc):
        return JobErrorCode.TIMEOUT
    if _is_cancel_error(exc):
        return JobErrorCode.CANCELLED
    if kind is JobKind.TEST_CONNECTION or isinstance(exc, DatasourceConnectionTestError):
        return JobErrorCode.CONNECTION_FAILED
    if isinstance(exc, AdapterConnectionError):
        return JobErrorCode.CONNECTION_FAILED
    if kind in {JobKind.SQL_QUERY, JobKind.SQL_EXPLAIN}:
        return JobErrorCode.SQL_FAILED
    if kind is JobKind.RESULT_EXPORT:
        return JobErrorCode.EXPORT_FAILED
    return JobErrorCode.INTERNAL


def _is_timeout_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timed out" in text or "timeout" in text


def _is_cancel_error(exc: Exception) -> bool:
    return isinstance(exc, JobCancelled) or "cancel" in type(exc).__name__.lower()


def _datasource_test_error_code(error_summary: object) -> str:
    if not isinstance(error_summary, str) or not error_summary:
        return "unknown"
    text = error_summary.lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "code=1045" in text or "access denied" in text or "authentication" in text:
        return "auth_failed"
    if (
        "code=2003" in text
        or "code=2005" in text
        or "can't connect" in text
        or "cannot connect" in text
        or "connection refused" in text
        or "name or service not known" in text
    ):
        return "host_unreachable"
    return "unknown"


if __name__ == "__main__":
    main()
