from __future__ import annotations

import io
import json
import os
import shutil
import signal
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from itertools import islice
from random import Random
from typing import Any, BinaryIO, Protocol, cast
from uuid import uuid4

import structlog
from sqlalchemy import URL, and_, create_engine, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config import Settings, load_settings
from app.db.models import (
    datasources,
    jobs,
    lineage_column_edges,
    lineage_edges,
    lineage_runs,
    metadata_caches,
    result_sets,
    run_index,
)
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.protocol import AdapterConnectionError
from app.dbclients.sql_build import limit_clause as _limit_clause
from app.dbclients.sql_build import quote_alias as _quote_alias
from app.dbclients.sql_build import quote_identifier as _quote_identifier
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
from app.domain.lineage import (
    LINEAGE_PARSER_VERSION,
    LineageParseRequest,
    LineageReport,
    analyze_sql_lineage,
    build_lineage_edge_rows,
    build_semantic_view,
    lineage_sql_hash,
    schema_from_metadata_cache_rows,
)
from app.domain.notify import RunNotification
from app.domain.plan import PlanNode
from app.domain.readers import (
    CsvReader,
    ExcelReader,
    MaxRowsExceededError,
    RowReader,
)
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import SecretKind, SecretRef
from app.domain.workflow import WorkflowNode, WorkflowSpec
from app.domain.workflow_execution import (
    WorkflowChildJob,
    plan_workflow_step,
)
from app.domain.workflow_interpolate import interpolate_payload
from app.domain.workflow_when import builtin_when_variables, when_variables_from_payload
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.result_export import (
    ExportFormat,
    ExportSizeLimitExceeded,
    write_result_export,
    write_xlsx_workbook,
)
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.observability.logging import configure_logging
from app.services.notify import RevealSecret, WorkflowNotifyService

logger = structlog.get_logger(__name__)

# lineage_batch ZIP 安全限额(zip 炸弹 / 超量防护)
_LINEAGE_BATCH_MAX_FILES = 500
_LINEAGE_BATCH_MAX_FILE_BYTES = 2 * 1024 * 1024
_LINEAGE_BATCH_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_LINEAGE_BATCH_EXTENSIONS = (".sql", ".txt")

# 文件源对比全量物化的行数硬顶(C-1):防大文件把 worker 撑爆内存。
# 任务 run_limits.max_rows 若给出则以其为准,否则用本默认;超限直接 failed(不截断)。
_FILE_COMPARE_DEFAULT_MAX_ROWS = 1_000_000

# Excel 单 sheet 硬上限 1,048,576 行(含表头);数据行上限 = 减去表头 1 行。
# compare 结果导出每 sheet 必须 cap 到此值,否则运维调高 spool_max_rows 即产出非法 xlsx。
_EXCEL_MAX_DATA_ROWS_PER_SHEET = 1_048_575

# B 组明细持久化封顶(L-5+ PR2):富明细叠加大批量会撑爆 report JSON,
# 必须双重封顶——每文件每类明细行上限 + 全局总行预算;超限截断并打 details_truncated
# 标记 + 结构化 log 丢弃了什么(不静默膨胀 / 不静默截断)。
_LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE = 2000
_LINEAGE_BATCH_DETAIL_MAX_TOTAL_ROWS = 50_000
# 单明细字段字符上限(过程段 sql / 动态 SQL 片段等可能很长):JSON 侧先截断,
# 与 exporter _safe_text 的 Excel 单元格截断互补,双保险防 JSON 膨胀。
_LINEAGE_BATCH_DETAIL_MAX_CELL_CHARS = 4000
# 每类明细持久化的键投影(只留 B 组 sheet 用得到的列,砍冗余降体积);
# procedure_segments / dynamic_sql_segments 形态未定(2.0 解析器暂未产出)→ 原样透传。
_LINEAGE_BATCH_DETAIL_KINDS = (
    "procedure_segments",
    "statements",
    "parse_errors",
    "dynamic_sql_segments",
    "warnings",
    "insert_mappings",
    "graph_edges",
)
_LINEAGE_BATCH_DETAIL_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "statements": ("index", "type"),
    "parse_errors": ("statement_index", "error_type", "message", "statement_type", "unsupported"),
    "warnings": ("code", "message", "statement_index"),
    "insert_mappings": (
        "target_table",
        "target_column",
        "source_table",
        "source_column",
        "transformation",
        "transformation_subtype",
        "statement_index",
    ),
    "graph_edges": ("source_table", "target_table", "statement_index", "inferred"),
}


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


class WorkflowRunFailedError(RuntimeError):
    """WorkflowRun 终态失败(节点失败 / abort);message 为公开错误串。"""


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

    # workflow_run 编排(2.4.0 PR-4)
    def enqueue(self, job: Job) -> None: ...

    def request_cancel(self, job_id: str) -> None: ...

    def list_jobs_by_parent(self, parent_workflow_run_id: str) -> list[Job]: ...

    def requeue_workflow_run(self, job_id: str) -> None: ...

    def retry_workflow_node(self, job_id: str) -> None: ...

    def cancel_pending_job(self, job_id: str, reason: str = "cancelled") -> None: ...


class ResultStoreLike(Protocol):
    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]: ...

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None: ...

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef: ...

    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef: ...

    def open_download(self, ref: ResultRef) -> BinaryIO: ...

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


class LineageCatalogLike(Protocol):
    """lineage_analyze 子 job 的落库依赖(workflow 节点执行用,2.4.0 PR-4)。"""

    def schema_context(
        self,
        datasource_id: str,
        default_schema: str | None,
    ) -> dict[str, Any]: ...

    def cached_run_id(
        self,
        *,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
    ) -> str | None: ...

    def persist_run(
        self,
        *,
        run_id: str,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
        sql_text: str,
        report: LineageReport,
    ) -> None: ...


DatasourceLoader = Callable[[str], DatasourceConnInfo]
AdapterFactory = Callable[
    [DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]],
    DatabaseAdapterLike,
]


@dataclass(frozen=True)
class WorkerRunnerConfig:
    worker_id: str
    heartbeat_interval_seconds: float = 15.0
    heartbeat_timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0
    sql_spool_batch_size: int = 1000
    cancel_check_row_interval: int = 5000
    result_gc_interval_seconds: float = 600.0
    export_limit_mb: int = 1024
    # workflow_run 推进器:一步无进展时让位前的节流睡眠(避免 claim/requeue 热循环)
    workflow_advance_interval_seconds: float = 1.0
    # RetryPolicy=None 时继承的全局重试次数(与 PostgresJobBackend
    # job_default_max_retries 同口径;当前部署默认 0 = 不重试)
    workflow_node_default_max_retries: int = 0


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
        lineage_catalog: LineageCatalogLike | None = None,
        notify_reveal: RevealSecret | None = None,
    ) -> None:
        if config.sql_spool_batch_size <= 0:
            raise ValueError("sql_spool_batch_size must be positive")
        if config.cancel_check_row_interval <= 0:
            raise ValueError("cancel_check_row_interval must be positive")
        if config.result_gc_interval_seconds <= 0:
            raise ValueError("result_gc_interval_seconds must be positive")
        if config.export_limit_mb <= 0:
            raise ValueError("export_limit_mb must be positive")
        if config.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if config.workflow_advance_interval_seconds <= 0:
            raise ValueError("workflow_advance_interval_seconds must be positive")
        if config.workflow_node_default_max_retries < 0:
            raise ValueError("workflow_node_default_max_retries must be non-negative")
        self._backend = backend
        self._result_store = result_store
        self._datasource_loader = datasource_loader
        self._adapter_factory = adapter_factory
        self._config = config
        self._result_set_catalog = result_set_catalog
        self._job_error_code_writer = job_error_code_writer
        self._compare_run_catalog = compare_run_catalog
        self._lineage_catalog = lineage_catalog
        # C-9:run 终态通知服务(注入 SecretStore.reveal_secret 作 RevealSecret DI)。
        # 未注入 reveal = 不通知(no-op),不影响 run 终态落定。
        self._notify_service = (
            WorkflowNotifyService(notify_reveal) if notify_reveal is not None else None
        )
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
        outcome: _ExecutionOutcome | None
        heartbeat_stop, heartbeat_thread = self._start_job_heartbeat_thread(job.id)
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
            elif job.kind is JobKind.EXPORT_EXCEL:
                outcome = self._execute_export_excel(job)
            elif job.kind is JobKind.COMPARE_RUN:
                outcome = self._execute_compare_run(job)
            elif job.kind is JobKind.LINEAGE_ANALYZE:
                outcome = self._execute_lineage_analyze(job)
            elif job.kind is JobKind.LINEAGE_BATCH:
                outcome = self._execute_lineage_batch(job)
            elif job.kind is JobKind.WORKFLOW_RUN:
                outcome = self._execute_workflow_run(job)
            else:
                raise UnsupportedJobKindError(f"Unsupported job kind: {job.kind.value}")
        except JobCancelled as exc:
            if job.kind is JobKind.WORKFLOW_RUN:
                # run 取消传播:运行中子 job 软取消,pending 子 job 直接终态
                self._cancel_workflow_children(job.id)
            self._backend.mark_cancelled(job.id, str(exc))
            self._write_result_set_terminal(job, "closed")
            self._write_compare_run_terminal(job, JobStatus.CANCELLED.value)
            self._notify_workflow_run_terminal(job, JobStatus.CANCELLED.value)
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
            self._notify_workflow_run_terminal(job, JobStatus.FAILED.value, error=public_error)
            logger.exception(
                "worker job failed",
                job_id=job.id,
                kind=job.kind.value,
                error_code=error_code.value,
                error_type=type(exc).__name__,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        else:
            if outcome is None:
                # workflow_run 推进一步后让位(已 requeue 回 pending),
                # 不是终态,不写 complete/fail。
                logger.info(
                    "worker workflow run yielded",
                    job_id=job.id,
                    elapsed_seconds=_elapsed_seconds(started_at),
                )
                return
            self._backend.complete(job.id, outcome.result_ref)
            self._notify_workflow_run_terminal(job, JobStatus.SUCCESS.value)
            logger.info(
                "worker job complete",
                job_id=job.id,
                kind=job.kind.value,
                loaded_rows=outcome.loaded_rows,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)

    def _start_job_heartbeat_thread(self, job_id: str) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_until_stopped,
            args=(job_id, stop),
            name=f"dataops-heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def _heartbeat_until_stopped(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self._config.heartbeat_interval_seconds):
            try:
                self._heartbeat(job_id)
            except Exception as exc:
                logger.warning(
                    "worker job heartbeat failed",
                    job_id=job_id,
                    worker_id=self._config.worker_id,
                    error_type=type(exc).__name__,
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
        if "compare_run_id" in payload:
            return self._execute_compare_result_export(job)
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

    def _execute_compare_result_export(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        run_id = _required_payload_str(payload, "compare_run_id")
        export_format = _payload_export_format(payload)
        if export_format != "excel":
            raise ValueError("compare result export only supports excel")
        filename = _required_payload_str(payload, "filename")
        bucket_spools = _payload_export_bucket_result_set_ids(payload)
        limit_bytes = self._config.export_limit_mb * 1024 * 1024
        # 每 sheet 行数上限:cap 到 min(export_max_rows, Excel 单 sheet 硬顶),
        # 否则 spool_max_rows 调高后单 sheet 会超 1,048,576 → 非法 xlsx。
        row_cap = _compare_export_row_cap(payload)
        loaded_rows = 0
        sheets: list[tuple[str, list[Column], Iterable[Row]]] = []
        for bucket in COMPARE_BUCKETS:
            result_set_id = bucket_spools[bucket]
            manifest = self._result_store.get_spool_manifest(result_set_id)
            loaded_rows += _manifest_int(manifest, "loaded_rows")
            sheets.append(
                (
                    bucket,
                    _columns_from_manifest(manifest),
                    islice(self._iter_spool_rows(job.id, result_set_id), row_cap),
                )
            )
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024, mode="w+b") as stream:
            binary_stream = cast(BinaryIO, stream)
            exported_bytes = write_xlsx_workbook(
                stream=binary_stream,
                sheets=sheets,
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
                    "compare_run_id": run_id,
                    "bucket_result_set_ids": bucket_spools,
                }
            }
        )
        return _ExecutionOutcome(result_ref=result_ref, loaded_rows=loaded_rows)

    def _execute_compare_run(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        run_id = _required_payload_str(payload, "run_id")
        source_id = _payload_optional_str(payload, "source_id")
        target_id = _payload_optional_str(payload, "target_id")
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

        source_ref = _payload_compare_data_ref(payload, "source_ref")
        target_ref = _payload_compare_data_ref(payload, "target_ref")
        # 文件侧(C-1)天然走"客户端哈希 + 全量物化",不下推/不分段/不流式:
        # 任一侧 file 即整体走内存全量 diff(也解决跨类型/无序问题)。
        file_mode = source_ref.get("kind") == "file" or target_ref.get("kind") == "file"
        max_rows = limits.max_rows or _FILE_COMPARE_DEFAULT_MAX_ROWS

        source_reader = self._build_compare_reader(
            job,
            data_ref=source_ref,
            datasource_id=source_id,
            key_columns=key_columns,
            value_columns=compare_columns,
            select_key_names=[column.name for column in key_columns],
            select_value_names=[column.name for column in compare_columns],
            rules=rules,
            max_rows=max_rows,
        )
        target_reader = self._build_compare_reader(
            job,
            data_ref=target_ref,
            datasource_id=target_id,
            key_columns=key_columns,
            value_columns=compare_columns,
            select_key_names=[
                rules.column_mappings.get(column.name, column.name) for column in key_columns
            ],
            select_value_names=[
                rules.column_mappings.get(column.name, column.name) for column in compare_columns
            ],
            rules=rules,
            max_rows=max_rows,
        )

        effective_limits = (
            limits.model_copy(update={"recursive_checksum": False})
            if limits.persist_same_bucket
            else limits
        )
        started_at = time.monotonic()
        sample_result: dict[str, object] | None = None
        if file_mode:
            # 全量物化对比:两侧 fetch_all 后按归一化 key 对齐(跨类型/大小写等由
            # CompareRules 规范化吸收),不走 DB 下推的分段/采样路径。
            result = _diff_materialized_rows(
                source_reader.fetch_all(),
                target_reader.fetch_all(),
                key_columns=key_columns,
                rules=rules,
            )
        elif limits.sample_quick_check:
            result, sample_result = _run_compare_sample_quick_check(
                source_reader=cast(_DatabaseCompareReader, source_reader),
                target_reader=cast(_DatabaseCompareReader, target_reader),
                key_columns=key_columns,
                limits=limits,
                run_id=run_id,
            )
        else:
            result = _run_compare_hashdiff(
                source_reader=cast(_DatabaseCompareReader, source_reader),
                target_reader=cast(_DatabaseCompareReader, target_reader),
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

    def _build_compare_reader(
        self,
        job: Job,
        *,
        data_ref: dict[str, object],
        datasource_id: str | None,
        key_columns: list[CompareColumn],
        value_columns: list[CompareColumn],
        select_key_names: list[str],
        select_value_names: list[str],
        rules: CompareRules,
        max_rows: int,
    ) -> _DatabaseCompareReader | _FileCompareReader:
        """按 ref.kind 装配对比读取器:file → 全量物化内存读;table/sql → DB 读。"""

        if data_ref.get("kind") == "file":
            rows = self._materialize_file_rows(data_ref, max_rows=max_rows)
            return _FileCompareReader(
                rows=rows,
                value_columns=value_columns,
                select_key_names=select_key_names,
                select_value_names=select_value_names,
                rules=rules,
            )
        if datasource_id is None:
            raise ValueError("compare db side requires a datasource id")
        datasource = self._datasource_loader(datasource_id)
        _require_operation_allowed(datasource.operation_policy, "select")
        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )
        return _DatabaseCompareReader(
            adapter=adapter,
            datasource=datasource,
            data_ref=data_ref,
            key_columns=key_columns,
            value_columns=value_columns,
            select_key_names=select_key_names,
            select_value_names=select_value_names,
            rules=rules,
        )

    def _materialize_file_rows(
        self,
        data_ref: dict[str, object],
        *,
        max_rows: int,
    ) -> list[dict[str, Any]]:
        """上传件下载到临时文件 → domain reader 全量物化(超 max_rows 硬顶 raise)。

        临时文件用完即删(finally);行数据不进日志(R5)。openpyxl 靠扩展名判
        格式,故临时文件保留原始后缀。
        """

        storage_uri = data_ref.get("storage_uri")
        if not isinstance(storage_uri, str) or not storage_uri:
            raise ValueError("file compare ref requires storage_uri")
        filename = data_ref.get("filename")
        suffix = os.path.splitext(filename)[1] if isinstance(filename, str) else ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            with self._result_store.open_download(
                ResultRef(backend="local_fs", uri=storage_uri)
            ) as stream:
                shutil.copyfileobj(stream, tmp)
        try:
            reader = _build_domain_file_reader(data_ref, tmp_path)
            return reader.fetch_all(max_rows=max_rows)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _execute_export_excel(self, job: Job) -> _ExecutionOutcome:
        """export_excel = result_export 固定 format=excel 的薄封装(workflow 节点用)。"""
        payload = dict(job.payload)
        payload["format"] = "excel"
        return self._execute_result_export(job.model_copy(update={"payload": payload}))

    def _execute_lineage_analyze(self, job: Job) -> _ExecutionOutcome:
        """lineage_analyze 薄 handler:确定性解析 + 落 lineage_runs/edges。

        复用 API 侧 analyze 的 service 逻辑(app/domain/lineage 纯解析 +
        sql_hash 缓存语义);不含 AI 兜底(worker 侧只跑确定性主路径)。
        """
        if self._lineage_catalog is None:
            raise UnsupportedJobKindError("lineage_analyze requires a lineage catalog")
        payload = job.payload
        datasource_id = _payload_datasource_id(job)
        sql_text = _required_payload_str(payload, "sql_text")
        source_ref = _payload_optional_str(payload, "source_ref") or "workflow"
        default_schema = _payload_optional_str(payload, "default_schema")
        dialect = _payload_optional_str(payload, "dialect")
        if dialect is None:
            dialect = self._datasource_loader(datasource_id).db_type.value
        dialect = dialect.lower()
        schema_context = self._lineage_catalog.schema_context(datasource_id, default_schema)
        sql_hash = lineage_sql_hash(
            sql_text=sql_text,
            dialect=dialect,
            schema_context=schema_context,
            parser_version=LINEAGE_PARSER_VERSION,
        )
        cached_run_id = self._lineage_catalog.cached_run_id(
            project_id=job.project_id,
            datasource_id=datasource_id,
            dialect=dialect,
            source_ref=source_ref,
            sql_hash=sql_hash,
        )
        if cached_run_id is not None:
            return _ExecutionOutcome(
                ResultRef(
                    backend="lineage_run",
                    uri=f"lineage/{cached_run_id}",
                    metadata={"lineage_run_id": cached_run_id, "cached": True},
                )
            )
        report = analyze_sql_lineage(
            LineageParseRequest.model_validate(
                {
                    "sql_text": sql_text,
                    "dialect": dialect,
                    "schema": schema_context["schema"],
                    "default_schema": default_schema,
                }
            )
        )
        run_id = str(uuid4())
        self._lineage_catalog.persist_run(
            run_id=run_id,
            project_id=job.project_id,
            datasource_id=datasource_id,
            dialect=dialect,
            source_ref=source_ref,
            sql_hash=sql_hash,
            sql_text=sql_text,
            report=report,
        )
        self._heartbeat(job.id)
        return _ExecutionOutcome(
            ResultRef(
                backend="lineage_run",
                uri=f"lineage/{run_id}",
                metadata={
                    "lineage_run_id": run_id,
                    "cached": False,
                    "table_edge_count": len(report.graph_edges),
                    "column_edge_count": len(report.insert_mappings),
                    "parse_error_count": len(report.parse_errors),
                },
            )
        )

    def _execute_lineage_batch(self, job: Job) -> _ExecutionOutcome:
        """ZIP 批量血缘分析(L-2):逐文件宽松解析 + 落边 + 汇总报告。

        - 宽松模式(lenient=True):引用表缺元数据缓存时降级出表级血缘,
          不再"缺表=零边"(1.x 批量就是表级精度)
        - 命中 sql_hash 缓存的文件仍在内存重析一次取读写表(供跨脚本依赖),
          但不重复落 run/edges
        - 跨脚本依赖 script_edges:文件 A 写的表 ∩ 文件 B 读的表
        - 汇总报告 JSON 落 job artifact,由 API 报告端点回读
        """
        if self._lineage_catalog is None:
            raise UnsupportedJobKindError("lineage_batch requires a lineage catalog")
        payload = job.payload
        # 数据源可选:无库文本导入时 datasource_id 为空 → 纯宽松表级,不落子图
        datasource_id = _payload_optional_str(payload, "datasource_id")
        storage_uri = _required_payload_str(payload, "storage_uri")
        default_schema = _payload_optional_str(payload, "default_schema")
        dialect = _payload_optional_str(payload, "dialect")
        if datasource_id is not None:
            if dialect is None:
                dialect = self._datasource_loader(datasource_id).db_type.value
            schema_context = self._lineage_catalog.schema_context(datasource_id, default_schema)
        else:
            # 无数据源:dialect 必给(API 已校验),schema 为空 → 全程宽松表级,不持久化
            dialect = dialect or "oracle"
            schema_context = {"schema": {}, "default_schema": default_schema}
        dialect = dialect.lower()

        files_report: list[dict[str, Any]] = []
        skipped: dict[str, int] = {"non_sql": 0, "too_large": 0, "over_file_limit": 0}
        total_bytes = 0
        with self._result_store.open_download(
            ResultRef(backend="local_fs", uri=storage_uri)
        ) as stream:
            with zipfile.ZipFile(stream) as archive:
                members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir() and not info.filename.startswith("__MACOSX")
                ]
                eligible = [
                    info
                    for info in members
                    if info.filename.lower().endswith(_LINEAGE_BATCH_EXTENSIONS)
                ]
                skipped["non_sql"] = len(members) - len(eligible)
                if len(eligible) > _LINEAGE_BATCH_MAX_FILES:
                    skipped["over_file_limit"] = len(eligible) - _LINEAGE_BATCH_MAX_FILES
                    eligible = eligible[:_LINEAGE_BATCH_MAX_FILES]
                for info in eligible:
                    self._check_cancel(job.id)
                    if (
                        info.file_size > _LINEAGE_BATCH_MAX_FILE_BYTES
                        or total_bytes + info.file_size > _LINEAGE_BATCH_MAX_TOTAL_BYTES
                    ):
                        skipped["too_large"] += 1
                        continue
                    raw = archive.read(info)
                    total_bytes += len(raw)
                    files_report.append(
                        self._lineage_batch_file(
                            job=job,
                            datasource_id=datasource_id,
                            dialect=dialect,
                            default_schema=default_schema,
                            schema_context=schema_context,
                            source_ref=info.filename,
                            raw=raw,
                        )
                    )
        script_edges = _lineage_batch_script_edges(files_report)
        parsed = sum(1 for item in files_report if item["status"] in {"parsed", "cached"})
        failed = sum(1 for item in files_report if item["status"] == "failed")
        # L-4 语义视图:跨脚本聚合目标表整合/刷新模式/角色/观察/风险。
        # 消费内存透传的 _report,序列化前 pop 掉(不进 report JSON)。
        semantic_reports: list[tuple[str, LineageReport]] = [
            (str(item["source_ref"]), item["_report"])
            for item in files_report
            if isinstance(item.get("_report"), LineageReport)
        ]
        semantic_view = build_semantic_view(semantic_reports)
        # L-5+ PR2:序列化前把每文件富明细(B 组 sheet 数据源)投影进 files[].details,
        # 受双重封顶(每文件行上限 + 全局行预算);_report 内存对象在此 pop,不进 JSON。
        detail_budget = _LINEAGE_BATCH_DETAIL_MAX_TOTAL_ROWS
        details_dropped: list[dict[str, Any]] = []
        for item in files_report:
            report_obj = item.pop("_report", None)
            if not isinstance(report_obj, LineageReport):
                continue
            projected, detail_budget, dropped = _project_lineage_batch_details(
                report_obj, detail_budget
            )
            item["details"] = projected
            if dropped:
                details_dropped.append({"file": item.get("source_ref"), "dropped": dropped})
        if details_dropped:
            # 明确 log 丢弃了什么(不静默截断):哪些文件哪类明细从多少截到多少。
            logger.warning(
                "lineage_batch_details_truncated",
                job_id=job.id,
                total_row_budget=_LINEAGE_BATCH_DETAIL_MAX_TOTAL_ROWS,
                budget_remaining=detail_budget,
                truncated_files=details_dropped,
            )
        report_payload = {
            "file_count": len(files_report),
            "parsed": parsed,
            "failed": failed,
            "skipped": skipped,
            "table_edge_total": sum(
                int(item.get("table_edge_count") or 0) for item in files_report
            ),
            "column_mapping_total": sum(
                int(item.get("column_mapping_count") or 0) for item in files_report
            ),
            "files": files_report,
            "script_edges": script_edges,
            "semantic_view": semantic_view.model_dump(mode="json"),
            # 任一文件任一类明细发生截断即置真,供导出 / 前端提示"明细不完整"。
            "details_truncated": bool(details_dropped),
        }
        artifact_ref = self._result_store.put_artifact(
            job.id,
            "lineage_batch_report.json",
            io.BytesIO(json.dumps(report_payload, ensure_ascii=False).encode("utf-8")),
        )
        self._heartbeat(job.id)
        return _ExecutionOutcome(
            ResultRef(
                backend="lineage_batch",
                uri=f"lineage-batch/{job.id}",
                metadata={
                    "file_count": len(files_report),
                    "parsed": parsed,
                    "failed": failed,
                    "script_edge_count": len(script_edges),
                    "report_uri": artifact_ref.uri,
                },
            )
        )

    def _lineage_batch_file(
        self,
        *,
        job: Job,
        datasource_id: str | None,
        dialect: str,
        default_schema: str | None,
        schema_context: dict[str, Any],
        source_ref: str,
        raw: bytes,
    ) -> dict[str, Any]:
        assert self._lineage_catalog is not None
        sql_text = _decode_sql_bytes(raw)
        if sql_text is None or not sql_text.strip():
            return {"source_ref": source_ref, "status": "failed", "error": "undecodable_or_empty"}
        report = analyze_sql_lineage(
            LineageParseRequest.model_validate(
                {
                    "sql_text": sql_text,
                    "dialect": dialect,
                    "schema": schema_context["schema"],
                    "default_schema": default_schema,
                    "lenient": True,
                }
            )
        )
        run_id: str | None = None
        if datasource_id is not None:
            # 有数据源:落 lineage_runs/edges(可查询子图),复用 sql_hash 缓存
            sql_hash = lineage_sql_hash(
                sql_text=sql_text,
                dialect=dialect,
                schema_context=schema_context,
                parser_version=LINEAGE_PARSER_VERSION,
            )
            cached_run_id = self._lineage_catalog.cached_run_id(
                project_id=job.project_id,
                datasource_id=datasource_id,
                dialect=dialect,
                source_ref=source_ref,
                sql_hash=sql_hash,
            )
            if cached_run_id is not None:
                run_id = cached_run_id
                status = "cached"
            else:
                run_id = str(uuid4())
                self._lineage_catalog.persist_run(
                    run_id=run_id,
                    project_id=job.project_id,
                    datasource_id=datasource_id,
                    dialect=dialect,
                    source_ref=source_ref,
                    sql_hash=sql_hash,
                    sql_text=sql_text,
                    report=report,
                )
                status = "parsed"
        else:
            # 无数据源(纯文本导入):只出报告,不持久化(lineage_runs 需 FK)
            status = "parsed"
        tables_written = sorted({summary.table for summary in report.target_summary})
        tables_read = sorted(
            {str(edge["source_table"]) for edge in report.graph_edges if edge.get("source_table")}
        )
        return {
            "source_ref": source_ref,
            "status": status,
            "run_id": run_id,
            "table_edge_count": len(report.graph_edges),
            "column_mapping_count": len(report.insert_mappings),
            "parse_error_count": len(report.parse_errors),
            "lenient_statement_count": sum(
                1 for warning in report.warnings if warning.get("code") == "lenient_table_level"
            ),
            "tables_written": tables_written,
            "tables_read": tables_read,
            # 内存透传:供 L-4 语义视图聚合(序列化前 pop,不进 report JSON)
            "_report": report,
        }

    def _execute_workflow_run(self, job: Job) -> _ExecutionOutcome | None:
        """workflow_run 推进器:claim → 推进 DAG 一步 → 让位(返回 None)。

        ★ 架构关键(ADR-0009 + 单 worker 串行模型):worker 是单进程单槽位
        串行循环,workflow_run 若阻塞轮询子 job 会占住唯一槽位形成死锁
        (子 job 永远不被消费)。因此 run job 每次 claim 只推进一步:
        enqueue 就绪节点(子 job 优先级 = run+1,保证先于 run 被 claim)、
        重排到期重试、然后 requeue 自己回 pending 让出槽位;全节点终态时
        才 complete/fail。多 worker 部署同样成立,无需拓扑前提。
        """
        spec = _workflow_spec_from_payload(job.payload)
        children_jobs = self._backend.list_jobs_by_parent(job.id)
        # run 取消:传播后走统一 JobCancelled 路径
        self._check_cancel(job.id)
        children = _workflow_children_snapshots(children_jobs)
        now = datetime.now(UTC)
        # when 变量优先用触发时冻结的快照(与 API 状态查询同源,决策不随
        # 推进时刻漂移);在途旧 run 无快照时回退为按当前时刻计算
        frozen_variables = when_variables_from_payload(job.payload)
        # when 判定与 payload ${var} 插值共用同一份变量快照(口径不漂移)
        variables: Mapping[str, str | list[str]] = (
            frozen_variables if frozen_variables is not None else builtin_when_variables(now)
        )
        plan = plan_workflow_step(
            spec,
            children,
            now=now,
            default_max_retries=self._config.workflow_node_default_max_retries,
            when_variables=variables,
        )
        nodes_by_id = {node.id: node for node in spec.nodes}
        for node_id in plan.enqueue_node_ids:
            child_job = _build_workflow_child_job(job, nodes_by_id[node_id], variables)
            try:
                self._backend.enqueue(child_job)
            except IntegrityError:
                # uq_jobs_workflow_node_per_run 兜底:stale worker 苏醒等竞态下
                # 另一 worker 已 enqueue 同一节点,跳过防止节点双跑
                logger.warning(
                    "workflow node already enqueued by another worker, skipping",
                    job_id=job.id,
                    node_id=node_id,
                )
                continue
            logger.info(
                "workflow node enqueued",
                job_id=job.id,
                node_id=node_id,
                child_job_id=child_job.id,
                kind=child_job.kind.value,
            )
        for child_job_id in plan.retry_job_ids:
            self._backend.retry_workflow_node(child_job_id)
            logger.info(
                "workflow node retry requeued",
                job_id=job.id,
                child_job_id=child_job_id,
            )
        for child_job_id in plan.cancel_job_ids:
            self._backend.request_cancel(child_job_id)
            self._backend.cancel_pending_job(child_job_id, "workflow aborted")

        if plan.run_status is None:
            made_progress = bool(plan.enqueue_node_ids or plan.retry_job_ids)
            if not made_progress:
                # 无进展(子 job 在跑 / 等 backoff):节流后让位,避免热循环
                delay = self._config.workflow_advance_interval_seconds
                if plan.wait_seconds is not None:
                    delay = min(max(plan.wait_seconds, 0.05), delay)
                time.sleep(delay)
            self._heartbeat(job.id)
            self._backend.requeue_workflow_run(job.id)
            return None

        summary = {
            node_id: {
                "status": state.status.value,
                "job_id": state.job_id,
                "attempts": state.attempts,
                "error": state.error,
            }
            for node_id, state in plan.node_states.items()
        }
        if plan.run_status is JobStatus.FAILED:
            logger.info(
                "workflow run failed",
                job_id=job.id,
                error=plan.run_error,
                nodes=summary,
            )
            raise WorkflowRunFailedError(plan.run_error or "workflow failed")
        return _ExecutionOutcome(
            ResultRef(
                backend="workflow_run",
                uri=f"workflow/{job.id}",
                metadata={
                    "workflow_id": _payload_optional_str(job.payload, "workflow_id"),
                    "nodes": summary,
                },
            )
        )

    def _cancel_workflow_children(self, run_job_id: str) -> None:
        for child in self._backend.list_jobs_by_parent(run_job_id):
            if child.status in {JobStatus.PENDING, JobStatus.RUNNING}:
                self._backend.request_cancel(child.id)
                self._backend.cancel_pending_job(child.id, "workflow run cancelled")

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

    def _notify_workflow_run_terminal(
        self, job: Job, run_status: str, *, error: str | None = None
    ) -> None:
        """C-9:workflow run 到终态时发通知(webhook / 企业微信)。

        ★ 失败隔离(硬红线):run 终态已由 complete/fail/mark_cancelled 在**调用本方法之前**
        写入;本方法是纯 after-effect。整个方法包在 try/except,任何异常(spec 解析 / 通知
        构造 / 服务违约抛)都被吞掉并只记不含敏感值的 warning,**绝不回传 worker 循环、
        绝不改写 run 终态**。service 内部另有 per-target 失败隔离(#128),此处是外层兜底。

        - 仅对 ``JobKind.WORKFLOW_RUN`` 生效;未注入 reveal(``_notify_service is None``)= no-op。
        - ``error`` 已是 ``_public_error_message`` 的公开串(R5);``RunNotification`` 再截断兜底。
        - payload 只含非敏感元数据(workflow 名 / run id / 状态 / 公开错误摘要);
          webhook url/token 只在 service→channel 内经 reveal 取,绝不落 payload / 日志(R2/R5)。
        """
        if job.kind is not JobKind.WORKFLOW_RUN or self._notify_service is None:
            return
        try:
            spec = _workflow_spec_from_payload(job.payload)
            if not spec.notifications:
                return
            payload = RunNotification(
                workflow_id=_payload_optional_str(job.payload, "workflow_id") or job.id,
                workflow_name=_payload_optional_str(job.payload, "workflow_name"),
                project=job.project_id,
                run_job_id=job.id,
                trigger=_payload_optional_str(job.payload, "trigger"),
                status=run_status,
                finished_at=datetime.now(UTC),
                error=error,
            )
            self._notify_service.notify(spec, payload, run_status)
        except Exception:
            # 不打 url / error 明文 / stacktrace(R5):只留 job_id + run_status。
            logger.warning(
                "workflow run notify skipped",
                job_id=job.id,
                run_status=run_status,
            )

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


class PostgresLineageCatalog:
    """worker 侧 lineage_analyze 落库(与 API analyze 同表同缓存语义;无 AI 兜底)。"""

    _METADATA_LEVEL_COLUMNS = "columns"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def schema_context(
        self,
        datasource_id: str,
        default_schema: str | None,
    ) -> dict[str, Any]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(
                        metadata_caches.c.cache_level,
                        metadata_caches.c.schema_name,
                        metadata_caches.c.table_name,
                        metadata_caches.c.payload,
                    ).where(
                        and_(
                            metadata_caches.c.datasource_id == datasource_id,
                            metadata_caches.c.cache_level == self._METADATA_LEVEL_COLUMNS,
                        )
                    )
                )
                .mappings()
                .all()
            )
        schema = schema_from_metadata_cache_rows([dict(row) for row in rows])
        return {"default_schema": default_schema, "schema": schema}

    def cached_run_id(
        self,
        *,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
    ) -> str | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(lineage_runs.c.id).where(
                        and_(
                            lineage_runs.c.project_id == project_id,
                            lineage_runs.c.datasource_id == datasource_id,
                            lineage_runs.c.dialect == dialect,
                            lineage_runs.c.source_ref == source_ref,
                            lineage_runs.c.sql_hash == sql_hash,
                            lineage_runs.c.parser_version == LINEAGE_PARSER_VERSION,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return str(row["id"]) if row is not None else None

    def persist_run(
        self,
        *,
        run_id: str,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
        sql_text: str,
        report: LineageReport,
    ) -> None:
        now = datetime.now(UTC)
        parse_summary = dict(report.report or {})
        parse_summary["parser_version"] = LINEAGE_PARSER_VERSION
        table_rows, column_rows = build_lineage_edge_rows(
            run_id=run_id,
            project_id=project_id,
            sql_hash=sql_hash,
            graph_edges=report.graph_edges,
            insert_mappings=report.insert_mappings,
        )
        with self._engine.begin() as conn:
            conn.execute(
                insert(lineage_runs).values(
                    id=run_id,
                    project_id=project_id,
                    datasource_id=datasource_id,
                    dialect=dialect,
                    source_ref=source_ref,
                    sql_hash=sql_hash,
                    sql_text=sql_text,
                    parser_version=LINEAGE_PARSER_VERSION,
                    status="success",
                    parse_summary=parse_summary,
                    created_at=now,
                    updated_at=now,
                )
            )
            if table_rows:
                conn.execute(insert(lineage_edges), table_rows)
            if column_rows:
                conn.execute(insert(lineage_column_edges), column_rows)


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
        upload_ttl_hours=actual_settings.result_store.upload_ttl_hours,
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
            workflow_node_default_max_retries=(
                actual_settings.worker.workflow_node_default_max_retries
            ),
        ),
        PostgresResultSetCatalog(engine),
        PostgresJobErrorCodeWriter(engine),
        PostgresCompareRunCatalog(engine),
        PostgresLineageCatalog(engine),
        # C-9:注入 SecretStore.reveal_secret 作 RevealSecret DI(worker 已持 store 实例;
        # service 不自开 DB 连接,R1)。webhook url/token 只在发送前一刻经此解密。
        notify_reveal=secret_store.reveal_secret,
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


def _decode_sql_bytes(raw: bytes) -> str | None:
    """SQL 脚本解码:utf-8 优先,gbk 兜底(用户真实语料两种混存)。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _cap_lineage_detail_value(value: Any) -> Any:
    """单明细字段封顶:超长字符串截断(防 report JSON 被大 SQL 撑爆)。非字符串原样。"""
    if isinstance(value, str) and len(value) > _LINEAGE_BATCH_DETAIL_MAX_CELL_CHARS:
        return value[:_LINEAGE_BATCH_DETAIL_MAX_CELL_CHARS] + "…(truncated)"
    return value


def _project_lineage_detail_row(kind: str, row: Any) -> dict[str, Any]:
    """按 kind 投影一行明细:已知形态只留 B 组 sheet 用到的键;未知形态原样透传。

    两路都过 :func:`_cap_lineage_detail_value` 做单字段字符封顶。
    """
    if not isinstance(row, dict):
        return {}
    keys = _LINEAGE_BATCH_DETAIL_PROJECTIONS.get(kind)
    items = ((key, row.get(key)) for key in keys) if keys else row.items()
    return {key: _cap_lineage_detail_value(value) for key, value in items}


def _project_lineage_batch_details(
    report: LineageReport, budget: int
) -> tuple[dict[str, Any], int, list[str]]:
    """把一个文件的富明细投影成受封顶的 details dict(B 组 sheet 数据源)。

    双重封顶:每类明细 ≤ 每文件行上限,且全类合计不超剩余全局行预算。
    返回 ``(details, 剩余全局预算, 丢弃说明列表)``——details 含被截断类的
    ``{kind}_truncated: True`` 标记;丢弃说明形如 ``"insert_mappings:5000->2000"``
    供调用方 log。
    """
    details: dict[str, Any] = {}
    dropped: list[str] = []
    for kind in _LINEAGE_BATCH_DETAIL_KINDS:
        source = list(getattr(report, kind, None) or [])
        total = len(source)
        # 单类上限 = min(每文件上限, 剩余全局预算);预算耗尽则该类只出 0 行。
        cap = min(_LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE, max(budget, 0))
        kept = source[:cap]
        details[kind] = [_project_lineage_detail_row(kind, row) for row in kept]
        if len(kept) < total:
            details[f"{kind}_truncated"] = True
            dropped.append(f"{kind}:{total}->{len(kept)}")
        budget -= len(kept)
    return details, budget, dropped


def _lineage_batch_script_edges(files_report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨脚本依赖:文件 A 写的表被文件 B 读 → A→B(1.x script_edges 同义)。"""
    edges: list[dict[str, Any]] = []
    for producer in files_report:
        written = set(producer.get("tables_written") or [])
        if not written:
            continue
        for consumer in files_report:
            if consumer is producer:
                continue
            common = written & set(consumer.get("tables_read") or [])
            if common:
                edges.append(
                    {
                        "source_file": producer["source_ref"],
                        "target_file": consumer["source_ref"],
                        "tables": sorted(common),
                    }
                )
    return edges


def _workflow_spec_from_payload(payload: dict[str, Any]) -> WorkflowSpec:
    """载入 run payload 里的 spec 快照;model_validate 即 R7 执行期再校验。"""
    raw_spec = payload.get("spec")
    if not isinstance(raw_spec, dict):
        raise ValueError("workflow_run payload requires spec object")
    return WorkflowSpec.model_validate(raw_spec)


def _workflow_children_snapshots(children: list[Job]) -> dict[str, WorkflowChildJob]:
    """子 job → 节点快照;同一节点重试复用同一 job id,后写覆盖(按 created_at 序)。"""
    snapshots: dict[str, WorkflowChildJob] = {}
    for child in children:
        node_id = child.payload.get("workflow_node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        snapshots[node_id] = WorkflowChildJob(
            node_id=node_id,
            job_id=child.id,
            status=child.status,
            retry_count=child.retry_count,
            finished_at=child.finished_at,
            error=child.error,
        )
    return snapshots


def _build_workflow_child_job(
    run_job: Job, node: WorkflowNode, variables: Mapping[str, str | list[str]]
) -> Job:
    # 入队时刻渲染 payload 里的 ${var} 占位符(用触发时冻结的变量快照);
    # 未解析变量等确定性错误已在 plan_workflow_step 侧拦成节点 FAILED,故走到这里
    # 的节点插值必定成功。job_kind 只按原始 node.job_kind 构造,插值只碰 payload(R7)
    payload: dict[str, Any] = interpolate_payload(node.payload, variables)
    payload["workflow_node_id"] = node.id
    return Job(
        id=str(uuid4()),
        kind=JobKind(node.job_kind),
        status=JobStatus.PENDING,
        owner_user_id=run_job.owner_user_id,
        project_id=run_job.project_id,
        datasource_ids=_node_datasource_ids(payload),
        # 子 job 优先级高于 run job:单 worker 串行下保证子 job 先被 claim,
        # run 推进器不会饿死子 job(claim 序 = priority DESC, created_at ASC)。
        # run job = -1(见 trigger_workflow_run),故子 job = 0,与交互 sql_query
        # 同档,大工作流爆发时不压制交互查询(backlog Workflow minor #3)
        priority=run_job.priority + 1,
        timeout_seconds=node.timeout_seconds,
        resource_profile=ResourceProfile(timeout_seconds=node.timeout_seconds),
        audit_id=str(uuid4()),
        payload=payload,
        parent_workflow_run_id=run_job.id,
    )


def _node_datasource_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("datasource_id", "source_id", "target_id"):
        value = payload.get(key)
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    return ids


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


def _build_domain_file_reader(data_ref: dict[str, object], path: str) -> RowReader:
    """按 ref.file_format 装配 domain reader(与 file-preview 端点同口径)。"""

    file_format = data_ref.get("file_format")
    header_row = data_ref.get("header_row")
    header = int(header_row) if isinstance(header_row, int) else 1
    if file_format == "excel":
        sheet = data_ref.get("sheet")
        return ExcelReader(
            path,
            sheet=sheet if isinstance(sheet, str) else None,
            header_row=header,
        )
    if file_format == "csv":
        encoding = data_ref.get("encoding")
        delimiter = data_ref.get("delimiter")
        return CsvReader(
            path,
            encoding=encoding if isinstance(encoding, str) and encoding else "utf-8-sig",
            delimiter=delimiter if isinstance(delimiter, str) and delimiter else ",",
            header_row=header,
        )
    raise ValueError(f"unsupported compare file_format: {file_format!r}")


class _FileCompareReader:
    """文件源对比读取器:把已物化的 dict 行按任务列映射成 CompareRow。

    与 `_DatabaseCompareReader` 同 `fetch_all()` 契约,喂进内存全量 diff。pk 保留
    原始 key 值(供结果展示),值列走 `normalized_compare_identity` 规范化(跨源
    类型对齐由 CompareRules 吸收)。零 DB 依赖。
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        value_columns: list[CompareColumn],
        select_key_names: list[str],
        select_value_names: list[str],
        rules: CompareRules,
    ) -> None:
        self._rows = rows
        self._value_columns = value_columns
        self._select_key_names = select_key_names
        self._select_value_names = select_value_names
        self._rules = rules

    def fetch_all(self) -> list[CompareRow]:
        out: list[CompareRow] = []
        for row in self._rows:
            key_raw = tuple(row.get(name) for name in self._select_key_names)
            value_raw = tuple(row.get(name) for name in self._select_value_names)
            out.append(
                CompareRow(
                    pk=key_raw,
                    values=normalized_compare_identity(self._value_columns, value_raw, self._rules),
                    raw_values=value_raw,
                    row_hash64=compare_row_hash64(self._value_columns, value_raw, self._rules),
                )
            )
        return out


def _diff_materialized_rows(
    source_rows: list[CompareRow],
    target_rows: list[CompareRow],
    *,
    key_columns: list[CompareColumn],
    rules: CompareRules,
) -> HashdiffResult:
    """按归一化 key 对齐两侧全量行做 4 桶 diff(文件源路径)。

    与 `_diff_all_rows` 的差别:匹配键用 `normalized_compare_identity(key)` 而非
    原始 pk —— 让 CSV 的 "1"(str)与 DB 的 1(int)在 INTEGER/UNKNOWN 列下都能对齐。
    事件 pk 保留原始 key 值供展示。
    """

    def _match_key(row: CompareRow) -> tuple[tuple[bool, str], ...]:
        return normalized_compare_identity(key_columns, row.pk, rules)

    source_by_key = {_match_key(row): row for row in source_rows}
    target_by_key = {_match_key(row): row for row in target_rows}
    events: list[CompareDiffEvent] = []
    for key in sorted(source_by_key.keys() | target_by_key.keys()):
        source = source_by_key.get(key)
        target = target_by_key.get(key)
        if source is None and target is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_TARGET,
                    pk=target.pk,
                    target_values=target.raw_values or target.values,
                )
            )
        elif target is None and source is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_SOURCE,
                    pk=source.pk,
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
                    pk=source.pk,
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
    return _required_bucket_result_set_ids(raw, "compare_run payload")


def _payload_export_bucket_result_set_ids(payload: dict[str, object]) -> dict[str, str]:
    raw = payload.get("bucket_result_set_ids")
    if not isinstance(raw, dict):
        raise ValueError("compare export payload requires bucket_result_set_ids")
    return _required_bucket_result_set_ids(raw, "compare export payload")


def _compare_export_row_cap(payload: dict[str, object]) -> int:
    """每 sheet 导出行上限 = min(export_max_rows, Excel 单 sheet 硬顶)。

    export_max_rows(来自 RunLimits)缺省 / 非正整数时回退到 Excel 硬顶,
    保证任何配置下单 sheet 都不会超过 1,048,576 行(含表头)产出非法 xlsx。
    """
    raw = payload.get("export_max_rows")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return min(raw, _EXCEL_MAX_DATA_ROWS_PER_SHEET)
    return _EXCEL_MAX_DATA_ROWS_PER_SHEET


def _required_bucket_result_set_ids(raw: dict[object, object], label: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for bucket in COMPARE_BUCKETS:
        value = raw.get(bucket)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} missing bucket result set id: {bucket}")
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
    if kind not in {"table", "sql", "file"}:
        raise ValueError("compare ref kind must be table, sql or file")
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


# 标识符引用 / limit 子句抽至 app/dbclients/sql_build.py(API 预览端复用),
# 语义未变(compare 黄金一致性行为)。


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
    if isinstance(exc, MaxRowsExceededError):
        return "compare_limit_exceeded"
    if isinstance(exc, UnsupportedJobKindError):
        return "internal"
    if isinstance(exc, WorkflowRunFailedError):
        # message 由推进器构造(节点 id + 公开错误串),可直接透出
        return str(exc)
    if _is_timeout_error(exc):
        return "timeout"
    if _is_cancel_error(exc):
        return "cancelled"
    return _job_error_code(exc, kind).value


def _job_error_code(exc: Exception, kind: JobKind) -> JobErrorCode:
    if isinstance(exc, WorkflowRunFailedError):
        # 节点失败详情在 message / 子 job 各自的 error_code 里;run 级别统一 internal
        return JobErrorCode.INTERNAL
    if isinstance(exc, OperationPolicyDeniedError):
        return JobErrorCode.PERMISSION_DENIED
    if isinstance(exc, UnsupportedDbTypeError):
        return JobErrorCode.UNSUPPORTED_DB_TYPE
    if isinstance(exc, ExportSizeLimitExceeded):
        return JobErrorCode.EXPORT_LIMIT_EXCEEDED
    if isinstance(exc, MaxRowsExceededError):
        return JobErrorCode.COMPARE_LIMIT_EXCEEDED
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
