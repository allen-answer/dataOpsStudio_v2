from __future__ import annotations

import re
import signal
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from random import Random
from typing import Any, BinaryIO, Protocol, cast

import structlog
from sqlalchemy import URL, create_engine, insert, select, update
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import datasources, jobs, result_sets, run_index
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.protocol import AdapterConnectionError
from app.domain.compare import (
    CompareColumn,
    CompareDiffBucket,
    CompareDiffEvent,
    CompareHashLevel,
    CompareHashPlan,
    CompareHashRequest,
    CompareRow,
    CompareRules,
    CompareSegment,
    CompareTableRef,
    HashdiffProgress,
    HashdiffResult,
    RunLimits,
    SegmentFingerprint,
    compare_row_hash64,
    normalized_compare_identity,
    recursive_hashdiff,
)
from app.domain.compare_profile import (
    DEFAULT_SAMPLE_CONFIDENCE,
    DEFAULT_SAMPLE_SIZE,
    DiffProfileBuilder,
    empty_diff_profile,
    sample_quick_check_result,
)
from app.domain.compare_result import (
    COMPARE_BUCKETS,
    compare_result_columns,
    empty_bucket_counts,
    encode_compare_result_row,
)
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
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
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")


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

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan: ...


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


class CompareRunCatalogLike(Protocol):
    def write_running(
        self,
        *,
        run_id: str,
        bucket_spools: dict[str, str],
    ) -> None: ...

    def write_complete(
        self,
        *,
        run_id: str,
        bucket_counts: dict[str, int],
        progress: dict[str, int],
        diff_profile: dict[str, object],
        sample_result: dict[str, object] | None,
    ) -> None: ...

    def write_terminal(
        self,
        *,
        run_id: str,
        status: str,
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
        compare_run_catalog: CompareRunCatalogLike | None = None,
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
        self._compare_run_catalog = compare_run_catalog
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
            elif job.kind is JobKind.COMPARE_RUN:
                outcome = self._execute_compare_run(job)
            else:
                raise UnsupportedJobKindError(f"Unsupported job kind: {job.kind.value}")
        except JobCancelled as exc:
            self._backend.mark_cancelled(job.id, str(exc))
            self._write_result_set_terminal(job, "closed")
            self._write_compare_run_terminal(job, JobStatus.CANCELLED.value)
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
            self._write_compare_run_terminal(job, JobStatus.FAILED.value)
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

    def _execute_compare_run(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        run_id = _required_payload_str(payload, "run_id")
        source_id = _required_payload_str(payload, "source_id")
        target_id = _required_payload_str(payload, "target_id")
        bucket_spools = _payload_bucket_result_set_ids(payload)
        columns = _payload_compare_columns(payload)
        rules = _payload_compare_rules(payload)
        limits = _payload_run_limits(payload)
        key_columns = _key_columns(columns, rules)
        if not key_columns:
            raise ValueError("compare_run requires key_columns")
        compare_columns = _effective_compare_columns(columns, rules)
        if not compare_columns:
            raise ValueError("compare_run requires at least one compare column")

        for result_set_id in bucket_spools.values():
            self._result_store.set_spool_columns(result_set_id, compare_result_columns())
        self._write_compare_run_running(run_id, bucket_spools)

        source_ds = self._datasource_loader(source_id)
        target_ds = self._datasource_loader(target_id)
        _require_operation_allowed(source_ds.operation_policy, "select")
        _require_operation_allowed(target_ds.operation_policy, "select")

        source_adapter = self._adapter_factory(
            source_ds,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )
        target_adapter = self._adapter_factory(
            target_ds,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )
        source_ref = _payload_compare_data_ref(payload, "source_ref")
        target_ref = _payload_compare_data_ref(payload, "target_ref")
        source_reader = _DatabaseCompareReader(
            adapter=source_adapter,
            datasource=source_ds,
            data_ref=source_ref,
            key_columns=key_columns,
            value_columns=compare_columns,
            select_key_names=[column.name for column in key_columns],
            select_value_names=[column.name for column in compare_columns],
            rules=rules,
        )
        target_reader = _DatabaseCompareReader(
            adapter=target_adapter,
            datasource=target_ds,
            data_ref=target_ref,
            key_columns=key_columns,
            value_columns=compare_columns,
            select_key_names=[
                rules.column_mappings.get(column.name, column.name) for column in key_columns
            ],
            select_value_names=[
                rules.column_mappings.get(column.name, column.name) for column in compare_columns
            ],
            rules=rules,
        )

        effective_limits = (
            limits.model_copy(update={"recursive_checksum": False})
            if limits.persist_same_bucket
            else limits
        )
        started_at = time.monotonic()
        sample_result: dict[str, object] | None = None
        if limits.sample_quick_check:
            result, sample_result = _run_compare_sample_quick_check(
                source_reader=source_reader,
                target_reader=target_reader,
                key_columns=key_columns,
                limits=limits,
                run_id=run_id,
            )
        else:
            result = _run_compare_hashdiff(
                source_reader=source_reader,
                target_reader=target_reader,
                key_columns=key_columns,
                limits=effective_limits,
            )
        bucket_counts = empty_bucket_counts()
        bucket_counts[CompareDiffBucket.SAME.value] += result.progress.skipped_rows
        profile_builder: DiffProfileBuilder | None = DiffProfileBuilder(
            key_columns=key_columns,
            value_columns=compare_columns,
            rules=rules,
        )
        profile_error: str | None = None
        batches: dict[str, list[Row]] = {bucket: [] for bucket in COMPARE_BUCKETS}
        flushed_rows = 0
        flush_batch_size = min(self._config.sql_spool_batch_size, limits.compare_batch_size)
        last_heartbeat = time.monotonic()

        for event in result.events:
            bucket = event.bucket.value
            bucket_counts[bucket] += 1
            if profile_builder is not None:
                try:
                    profile_builder.observe(event)
                except Exception as exc:
                    profile_error = type(exc).__name__
                    profile_builder = None
                    logger.warning(
                        "compare diff profile generation disabled",
                        job_id=job.id,
                        run_id=run_id,
                        error_type=profile_error,
                    )
            if event.bucket is CompareDiffBucket.SAME and not limits.persist_same_bucket:
                continue
            batches[bucket].append(
                _compare_event_to_spool_row(
                    event,
                    key_columns=key_columns,
                    value_columns=compare_columns,
                    rules=rules,
                )
            )
            if len(batches[bucket]) >= flush_batch_size:
                self._flush_batch(job.id, bucket_spools[bucket], batches[bucket])
                flushed_rows += len(batches[bucket])
                batches[bucket] = []
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat, force=True)
            else:
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat)

        for bucket, batch in batches.items():
            if not batch:
                continue
            self._flush_batch(job.id, bucket_spools[bucket], batch)
            flushed_rows += len(batch)

        self._heartbeat(job.id)
        progress = {
            key: value
            for key, value in result.progress.model_dump(mode="json").items()
            if isinstance(value, int)
        }
        diff_profile = _finish_diff_profile(
            profile_builder=profile_builder,
            profile_error=profile_error,
            bucket_counts=bucket_counts,
            progress=progress,
            sample_result=sample_result,
            job_id=job.id,
            run_id=run_id,
        )
        self._write_compare_run_complete(
            run_id,
            bucket_counts,
            progress,
            diff_profile=diff_profile,
            sample_result=sample_result,
        )
        logger.info(
            "compare run spooled buckets",
            job_id=job.id,
            run_id=run_id,
            only_source=bucket_counts[CompareDiffBucket.ONLY_SOURCE.value],
            only_target=bucket_counts[CompareDiffBucket.ONLY_TARGET.value],
            diff=bucket_counts[CompareDiffBucket.DIFF.value],
            same=bucket_counts[CompareDiffBucket.SAME.value],
            flushed_rows=flushed_rows,
            elapsed_seconds=_elapsed_seconds(started_at),
        )
        return _ExecutionOutcome(
            result_ref=ResultRef(
                backend="compare_run",
                uri=f"compare/{run_id}",
                metadata={
                    "run_id": run_id,
                    "bucket_counts": bucket_counts,
                    "progress": progress,
                    "diff_profile": diff_profile,
                    "sample_result": sample_result,
                },
            ),
            loaded_rows=sum(bucket_counts.values()),
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

    def _write_compare_run_running(self, run_id: str, bucket_spools: dict[str, str]) -> None:
        if self._compare_run_catalog is None:
            return
        self._compare_run_catalog.write_running(run_id=run_id, bucket_spools=bucket_spools)

    def _write_compare_run_complete(
        self,
        run_id: str,
        bucket_counts: dict[str, int],
        progress: dict[str, int],
        *,
        diff_profile: dict[str, object],
        sample_result: dict[str, object] | None,
    ) -> None:
        if self._compare_run_catalog is None:
            return
        self._compare_run_catalog.write_complete(
            run_id=run_id,
            bucket_counts=bucket_counts,
            progress=progress,
            diff_profile=diff_profile,
            sample_result=sample_result,
        )

    def _write_compare_run_terminal(self, job: Job, status: str) -> None:
        if job.kind is not JobKind.COMPARE_RUN:
            return
        run_id = _payload_optional_str(job.payload, "run_id")
        if run_id is None or self._compare_run_catalog is None:
            return
        self._compare_run_catalog.write_terminal(run_id=run_id, status=status)

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


class PostgresCompareRunCatalog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write_running(self, *, run_id: str, bucket_spools: dict[str, str]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(run_index)
                .where(run_index.c.run_id == run_id)
                .values(
                    status=JobStatus.RUNNING.value,
                    bucket_spools=bucket_spools,
                    updated_at=datetime.now(UTC),
                )
            )

    def write_complete(
        self,
        *,
        run_id: str,
        bucket_counts: dict[str, int],
        progress: dict[str, int],
        diff_profile: dict[str, object],
        sample_result: dict[str, object] | None,
    ) -> None:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                update(run_index)
                .where(run_index.c.run_id == run_id)
                .values(
                    status=JobStatus.SUCCESS.value,
                    bucket_counts=bucket_counts,
                    progress=progress,
                    diff_profile=diff_profile,
                    sample_result=sample_result,
                    updated_at=now,
                    finished_at=now,
                )
            )

    def write_terminal(self, *, run_id: str, status: str) -> None:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                update(run_index)
                .where(run_index.c.run_id == run_id)
                .values(status=status, updated_at=now, finished_at=now)
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
        PostgresCompareRunCatalog(engine),
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


class _DatabaseCompareReader:
    def __init__(
        self,
        *,
        adapter: DatabaseAdapterLike,
        datasource: DatasourceConnInfo,
        data_ref: dict[str, object],
        key_columns: list[CompareColumn],
        value_columns: list[CompareColumn],
        select_key_names: list[str],
        select_value_names: list[str],
        rules: CompareRules,
    ) -> None:
        self._adapter = adapter
        self._datasource = datasource
        self._data_ref = data_ref
        self._key_columns = key_columns
        self._value_columns = value_columns
        self._select_key_names = select_key_names
        self._select_value_names = select_value_names
        self._rules = rules
        self._db_hash_disabled = False

    def bounds(self) -> tuple[int | None, int | None]:
        if len(self._key_columns) != 1:
            return None, None
        key_name = self._select_key_names[0]
        table_expr = _compare_table_expression(self._datasource.db_type, self._data_ref)
        key_expr = _quote_identifier(self._datasource.db_type, key_name)
        sql = f"SELECT MIN({key_expr}) AS min_key, MAX({key_expr}) AS max_key FROM {table_expr}"
        rows = list(self._adapter.execute_select(sql, {}))
        if not rows or len(rows[0].values) < 2:
            return None, None
        min_key = _optional_int(rows[0].values[0])
        max_key = _optional_int(rows[0].values[1])
        return min_key, max_key

    def segment_fingerprint(self, segment: CompareSegment) -> SegmentFingerprint:
        if not self._db_hash_disabled and self._can_use_db_hash():
            try:
                return self._db_segment_fingerprint(segment)
            except Exception:
                self._db_hash_disabled = True
        rows = list(self.fetch_rows(segment))
        return SegmentFingerprint(
            row_count=len(rows),
            aggregate_hash=sum(row.row_hash64 or 0 for row in rows),
        )

    def fetch_rows(self, segment: CompareSegment | None) -> Iterable[CompareRow]:
        sql = self._row_query(segment)
        for row in self._adapter.execute_select(sql, {}):
            yield self._row_to_compare(row)

    def fetch_all(self) -> list[CompareRow]:
        return list(self.fetch_rows(None))

    def fetch_first_at_or_after(self, anchor: int) -> CompareRow | None:
        if len(self._key_columns) != 1:
            return None
        key_expr = _quote_identifier(self._datasource.db_type, self._select_key_names[0])
        rows = list(self._fetch_rows_with_where(f"{key_expr} >= {anchor}", limit=1))
        return rows[0] if rows else None

    def fetch_key(self, key: int) -> list[CompareRow]:
        if len(self._key_columns) != 1:
            return []
        segment = CompareSegment(
            key_column=self._key_columns[0].name,
            start=key,
            end=key + 1,
        )
        return list(self.fetch_rows(segment))

    def _can_use_db_hash(self) -> bool:
        if self._data_ref.get("kind", "table") != "table":
            return False
        if len(self._key_columns) != 1:
            return False
        if self._select_key_names != [column.name for column in self._key_columns]:
            return False
        if self._select_value_names != [column.name for column in self._value_columns]:
            return False
        return self._datasource.db_type in {DbType.MYSQL, DbType.DM}

    def _db_segment_fingerprint(self, segment: CompareSegment) -> SegmentFingerprint:
        table_name = self._data_ref.get("table_name")
        if not isinstance(table_name, str):
            raise ValueError("table compare ref requires table_name")
        schema_name = self._data_ref.get("schema_name")
        request = CompareHashRequest(
            table=CompareTableRef(
                schema_name=schema_name if isinstance(schema_name, str) else None,
                name=table_name,
            ),
            columns=self._value_columns,
            key_columns=self._key_columns,
            rules=self._rules,
            segment=segment,
            level=CompareHashLevel.AGGREGATE,
        )
        plan = self._adapter.build_compare_hash_query(request)
        if not plan.uses_database_hash or plan.sql is None:
            raise ValueError("compare db hash plan degraded to client hash")
        rows = list(self._adapter.execute_select(plan.sql, plan.params))
        if not rows or len(rows[0].values) < 2:
            return SegmentFingerprint(row_count=0, aggregate_hash=0)
        return SegmentFingerprint(
            row_count=int(rows[0].values[0] or 0),
            aggregate_hash=int(rows[0].values[1] or 0),
        )

    def _row_query(self, segment: CompareSegment | None) -> str:
        where_clause = None
        if segment is not None:
            key_expr = _quote_identifier(self._datasource.db_type, self._select_key_names[0])
            where_clause = f"{key_expr} >= {segment.start} AND {key_expr} < {segment.end}"
        return self._row_query_with_where(where_clause)

    def _fetch_rows_with_where(
        self,
        where_clause: str,
        *,
        limit: int | None = None,
    ) -> Iterable[CompareRow]:
        sql = self._row_query_with_where(where_clause, limit=limit)
        for row in self._adapter.execute_select(sql, {}):
            yield self._row_to_compare(row)

    def _row_query_with_where(
        self,
        where_clause: str | None,
        *,
        limit: int | None = None,
    ) -> str:
        table_expr = _compare_table_expression(self._datasource.db_type, self._data_ref)
        key_exprs = [
            _select_identifier(self._datasource.db_type, name, alias=column.name)
            for name, column in zip(self._select_key_names, self._key_columns, strict=True)
        ]
        value_exprs = [
            _select_identifier(self._datasource.db_type, name, alias=column.name)
            for name, column in zip(self._select_value_names, self._value_columns, strict=True)
        ]
        where_sql = f" WHERE {where_clause}" if where_clause else ""
        order_exprs = ", ".join(
            _quote_identifier(self._datasource.db_type, name) for name in self._select_key_names
        )
        limit_sql = _limit_clause(self._datasource.db_type, limit)
        return (
            f"SELECT {', '.join([*key_exprs, *value_exprs])} "
            f"FROM {table_expr}{where_sql} ORDER BY {order_exprs}{limit_sql}"
        )

    def _row_to_compare(self, row: Row) -> CompareRow:
        key_len = len(self._key_columns)
        expected_len = key_len + len(self._value_columns)
        if len(row.values) != expected_len:
            raise ValueError("compare row shape does not match task columns")
        pk = tuple(row.values[:key_len])
        raw_values = tuple(row.values[key_len:])
        return CompareRow(
            pk=pk,
            values=normalized_compare_identity(self._value_columns, raw_values, self._rules),
            raw_values=raw_values,
            row_hash64=compare_row_hash64(self._value_columns, raw_values, self._rules),
        )


def _run_compare_hashdiff(
    *,
    source_reader: _DatabaseCompareReader,
    target_reader: _DatabaseCompareReader,
    key_columns: list[CompareColumn],
    limits: RunLimits,
) -> HashdiffResult:
    if len(key_columns) != 1 or key_columns[0].type is not ColumnType.INTEGER:
        return _diff_all_rows(source_reader.fetch_all(), target_reader.fetch_all())
    source_min, source_max = source_reader.bounds()
    target_min, target_max = target_reader.bounds()
    bounds = [
        value for value in (source_min, source_max, target_min, target_max) if value is not None
    ]
    if not bounds:
        return _diff_all_rows([], [])
    root = CompareSegment(
        key_column=key_columns[0].name,
        start=min(bounds),
        end=max(bounds) + 1,
    )
    return recursive_hashdiff(source_reader, target_reader, root, limits)


def _run_compare_sample_quick_check(
    *,
    source_reader: _DatabaseCompareReader,
    target_reader: _DatabaseCompareReader,
    key_columns: list[CompareColumn],
    limits: RunLimits,
    run_id: str,
) -> tuple[HashdiffResult, dict[str, object]]:
    requested_rows = limits.sample_size or DEFAULT_SAMPLE_SIZE
    confidence = limits.sample_confidence or DEFAULT_SAMPLE_CONFIDENCE
    if len(key_columns) != 1 or key_columns[0].type is not ColumnType.INTEGER:
        sample_result = sample_quick_check_result(
            requested_rows=requested_rows,
            sampled_rows=0,
            observed_differences=0,
            confidence=confidence,
            mode="unsupported_key",
        )
        sample_result["error"] = "single_integer_pk_required"
        return HashdiffResult(events=[], progress=HashdiffProgress()), sample_result

    source_min, source_max = source_reader.bounds()
    target_min, target_max = target_reader.bounds()
    bounds = [
        value for value in (source_min, source_max, target_min, target_max) if value is not None
    ]
    if not bounds:
        sample_result = sample_quick_check_result(
            requested_rows=requested_rows,
            sampled_rows=0,
            observed_differences=0,
            confidence=confidence,
        )
        sample_result["error"] = "empty_bounds"
        return HashdiffResult(events=[], progress=HashdiffProgress()), sample_result

    start = min(bounds)
    end = max(bounds)
    sampled_keys = _sample_primary_keys(
        source_reader=source_reader,
        target_reader=target_reader,
        start=start,
        end=end,
        requested_rows=requested_rows,
        seed=_sample_seed(run_id),
    )
    events: list[CompareDiffEvent] = []
    for key in sampled_keys:
        events.extend(
            _diff_all_rows(
                source_reader.fetch_key(key),
                target_reader.fetch_key(key),
            ).events
        )

    observed_differences = sum(1 for event in events if event.bucket is not CompareDiffBucket.SAME)
    progress = HashdiffProgress(row_mode_segments=1 if events else 0)
    sample_result = sample_quick_check_result(
        requested_rows=requested_rows,
        sampled_rows=len(events),
        observed_differences=observed_differences,
        confidence=confidence,
    )
    sample_result["anchor_strategy"] = "pk_random_anchor_no_order_by_rand"
    return HashdiffResult(events=events, progress=progress), sample_result


def _sample_primary_keys(
    *,
    source_reader: _DatabaseCompareReader,
    target_reader: _DatabaseCompareReader,
    start: int,
    end: int,
    requested_rows: int,
    seed: int,
) -> list[int]:
    if end < start:
        return []
    rng = Random(seed)
    keys: set[int] = set()
    max_attempts = max(requested_rows * 10, requested_rows + 100)
    attempts = 0
    while len(keys) < requested_rows and attempts < max_attempts:
        attempts += 1
        anchor = rng.randint(start, end)
        for reader in (source_reader, target_reader):
            row = reader.fetch_first_at_or_after(anchor)
            key = _first_int_pk(row)
            if key is not None and start <= key <= end:
                keys.add(key)
                if len(keys) >= requested_rows:
                    break
    return sorted(keys)


def _sample_seed(run_id: str) -> int:
    digest = sha256(f"compare-sample:{run_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _first_int_pk(row: CompareRow | None) -> int | None:
    if row is None or len(row.pk) != 1:
        return None
    value = row.pk[0]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _finish_diff_profile(
    *,
    profile_builder: DiffProfileBuilder | None,
    profile_error: str | None,
    bucket_counts: dict[str, int],
    progress: dict[str, int],
    sample_result: dict[str, object] | None,
    job_id: str,
    run_id: str,
) -> dict[str, object]:
    if profile_builder is None:
        return empty_diff_profile(reason=profile_error, sample_result=sample_result)
    try:
        return profile_builder.finish(
            bucket_counts=bucket_counts,
            progress=progress,
            sample_result=sample_result,
        )
    except Exception as exc:
        logger.warning(
            "compare diff profile generation failed",
            job_id=job_id,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        return empty_diff_profile(reason=type(exc).__name__, sample_result=sample_result)


def _diff_all_rows(source_rows: list[CompareRow], target_rows: list[CompareRow]) -> HashdiffResult:
    source_by_pk = {row.pk: row for row in source_rows}
    target_by_pk = {row.pk: row for row in target_rows}
    events: list[CompareDiffEvent] = []
    for pk in sorted(source_by_pk.keys() | target_by_pk.keys()):
        source = source_by_pk.get(pk)
        target = target_by_pk.get(pk)
        if source is None and target is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_TARGET,
                    pk=pk,
                    target_values=target.raw_values or target.values,
                )
            )
        elif target is None and source is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_SOURCE,
                    pk=pk,
                    source_values=source.raw_values or source.values,
                )
            )
        elif source is not None and target is not None:
            bucket = (
                CompareDiffBucket.SAME if source.values == target.values else CompareDiffBucket.DIFF
            )
            events.append(
                CompareDiffEvent(
                    bucket=bucket,
                    pk=pk,
                    source_values=source.raw_values or source.values,
                    target_values=target.raw_values or target.values,
                )
            )
    return HashdiffResult(
        events=events,
        progress=HashdiffProgress(row_mode_segments=1 if events else 0),
    )


def _payload_bucket_result_set_ids(payload: dict[str, object]) -> dict[str, str]:
    raw = payload.get("bucket_result_set_ids")
    if not isinstance(raw, dict):
        raise ValueError("compare_run payload requires bucket_result_set_ids")
    out: dict[str, str] = {}
    for bucket in COMPARE_BUCKETS:
        value = raw.get(bucket)
        if not isinstance(value, str) or not value:
            raise ValueError(f"compare_run payload missing bucket result set id: {bucket}")
        out[bucket] = value
    return out


def _payload_compare_columns(payload: dict[str, object]) -> list[CompareColumn]:
    raw = payload.get("columns")
    if not isinstance(raw, list):
        raise ValueError("compare_run payload columns must be a list")
    columns = []
    for item in raw:
        column = Column.model_validate(item)
        columns.append(
            CompareColumn(
                name=column.name,
                type=column.type,
                driver_type=column.driver_type,
            )
        )
    return columns


def _payload_compare_rules(payload: dict[str, object]) -> CompareRules:
    raw = payload.get("compare_rules")
    if raw is None:
        return CompareRules()
    if not isinstance(raw, dict):
        raise ValueError("compare_run payload compare_rules must be an object")
    return CompareRules.model_validate(raw)


def _payload_run_limits(payload: dict[str, object]) -> RunLimits:
    raw = payload.get("run_limits")
    if raw is None:
        return RunLimits()
    if not isinstance(raw, dict):
        raise ValueError("compare_run payload run_limits must be an object")
    return RunLimits.model_validate(raw)


def _payload_compare_data_ref(payload: dict[str, object], key: str) -> dict[str, object]:
    raw = payload.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"compare_run payload {key} must be an object")
    kind = raw.get("kind", "table")
    if kind not in {"table", "sql"}:
        raise ValueError("compare ref kind must be table or sql")
    return dict(raw)


def _key_columns(columns: list[CompareColumn], rules: CompareRules) -> list[CompareColumn]:
    by_name = {column.name: column for column in columns}
    return [
        by_name.get(name, CompareColumn(name=name, type=ColumnType.UNKNOWN))
        for name in rules.key_columns
    ]


def _effective_compare_columns(
    columns: list[CompareColumn],
    rules: CompareRules,
) -> list[CompareColumn]:
    ignored = set(rules.ignore_columns)
    return [column for column in columns if column.name not in ignored]


def _compare_event_to_spool_row(
    event: CompareDiffEvent,
    *,
    key_columns: list[CompareColumn],
    value_columns: list[CompareColumn],
    rules: CompareRules,
) -> Row:
    source_values = event.source_values
    target_values = event.target_values
    source = _values_dict(value_columns, source_values)
    target = _values_dict(value_columns, target_values)
    cells = (
        _cell_diffs(value_columns, source_values, target_values, rules)
        if event.bucket is CompareDiffBucket.DIFF
        and source_values is not None
        and target_values is not None
        else []
    )
    return encode_compare_result_row(
        pk={column.name: event.pk[index] for index, column in enumerate(key_columns)},
        source=source,
        target=target,
        cells=cells,
    )


def _cell_diffs(
    columns: list[CompareColumn],
    source_values: tuple[object, ...],
    target_values: tuple[object, ...],
    rules: CompareRules,
) -> list[dict[str, object]]:
    source_identity = normalized_compare_identity(columns, source_values, rules)
    target_identity = normalized_compare_identity(columns, target_values, rules)
    cells: list[dict[str, object]] = []
    for index, column in enumerate(columns):
        if source_identity[index] == target_identity[index]:
            continue
        cells.append(
            {
                "column": column.name,
                "source": source_values[index],
                "target": target_values[index],
            }
        )
    return cells


def _values_dict(
    columns: list[CompareColumn],
    values: tuple[object, ...] | None,
) -> dict[str, object] | None:
    if values is None:
        return None
    return {column.name: values[index] for index, column in enumerate(columns)}


def _compare_table_expression(db_type: DbType, data_ref: dict[str, object]) -> str:
    kind = data_ref.get("kind", "table")
    if kind == "sql":
        sql = data_ref.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql compare ref requires sql")
        return f"({sql}) DATAOPS_COMPARE_SOURCE"
    table_name = data_ref.get("table_name")
    if not isinstance(table_name, str) or not table_name:
        raise ValueError("table compare ref requires table_name")
    schema_name = data_ref.get("schema_name")
    if isinstance(schema_name, str) and schema_name:
        return f"{_quote_identifier(db_type, schema_name)}.{_quote_identifier(db_type, table_name)}"
    return _quote_identifier(db_type, table_name)


def _select_identifier(db_type: DbType, name: str, *, alias: str) -> str:
    return f"{_quote_identifier(db_type, name)} AS {_quote_alias(db_type, alias)}"


def _quote_identifier(db_type: DbType, identifier: str) -> str:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")
    if db_type is DbType.MYSQL:
        return ".".join(f"`{part.replace('`', '``')}`" for part in identifier.split("."))
    return ".".join(
        f'"{part.replace(chr(34), chr(34) + chr(34)).upper()}"' for part in identifier.split(".")
    )


def _quote_alias(db_type: DbType, alias: str) -> str:
    if not alias or _IDENTIFIER_RE.fullmatch(alias) is None:
        raise ValueError("Invalid database identifier")
    if db_type is DbType.MYSQL:
        return f"`{alias.replace('`', '``')}`"
    return f'"{alias.replace(chr(34), chr(34) + chr(34))}"'


def _limit_clause(db_type: DbType, limit: int | None) -> str:
    if limit is None:
        return ""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if db_type is DbType.MYSQL:
        return f" LIMIT {limit}"
    return f" FETCH FIRST {limit} ROWS ONLY"


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


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
    if kind in {JobKind.SQL_QUERY, JobKind.SQL_EXPLAIN, JobKind.COMPARE_RUN}:
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
