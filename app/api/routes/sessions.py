"""SQL 控制台会话端点(Session Broker 设计 §3.1)。

范围线(设计 §6):只读纵切。语句 SQL 仍过 `validate_readonly_sql`(guard 一行不改),
写能力是阶段 B。

与 job 路径的关系:**两条路径长期共存**。本模块不改动 `/api/sql/execute` 与
`/api/jobs/*` 的任何行为;`console_session_enabled=false`、非会话方言、
或 broker 未装配时,这些端点如实拒绝(409),前端据此整体回落 job 路径(§3.3)。

错误码(设计 §3.1 的 409 语义族)——**HTTP 409 = "会话状态不允许这次变更"**:
`stale_session_epoch / session_not_active / session_lost / session_limit_reached /
statement_not_cancellable / session_mailbox_full`,外加本片为 §3.3 回退路由补的
`console_session_disabled / console_session_unsupported`。400 族沿用 `invalid_sql`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from sqlalchemy import insert
from sqlalchemy.engine import RowMapping

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError

# spool manifest 读取与 console/datasource 授权是两条路径共用的深模块;
# 复制一份等于给"两套轮询契约漂移"开门(设计 §3.2),故直接复用 core 的实现。
from app.api.routes.core import (
    _bool_from_manifest,
    _columns_from_manifest,
    _console_for_current_user,
    _datasource_for_current_user,
    _datetime_delta_ms,
    _dict_bool,
    _dict_datetime,
    _dict_int,
    _dict_str,
    _download_token_hash,
    _enforce_export_rate_limit,
    _export_filename,
    _int_from_manifest,
    _manifest_datetime,
    _manifest_optional_str,
    _manifest_pagination_mode,
    _preview_result_rows,
    _result_set_metadata_section,
    _result_set_row,
    _spool_manifest_or_none,
    poll_retry_after_ms,
)
from app.api.schemas import (
    ExportCreateRequest,
    ExportCreateResponse,
    JobExecutionMetrics,
    JobStageTimings,
    RowResponse,
    SessionAttachRequest,
    SessionCloseRequest,
    SessionResponse,
    StatementCancelRequest,
    StatementCancelResponse,
    StatementProgressResponse,
    StatementResultResponse,
    StatementSessionBlock,
    StatementSubmitRequest,
    StatementSubmitResponse,
)
from app.api.services import ApiServices, new_id
from app.broker import BrokerError, SessionBroker, SessionObservation
from app.broker.store import AttachRequest
from app.broker.wiring import SESSION_CAPABLE_DB_TYPES
from app.db.models import export_download_tokens
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.console_session import ConsoleSession, ConsoleStatement, ConsoleStatementState
from app.domain.datasource import DbType
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.infrastructure.result_export import export_content_type

logger = structlog.get_logger(__name__)

router = APIRouter()

# 409 语义族(设计 §3.1)。broker 抛的每个 code 都必须在这里有归属 ——
# 漏一个就会以 500 泄漏给前端,`test_broker_error_codes_all_map_to_conflict` 钉死。
_BROKER_CONFLICT_CODES = frozenset(
    {
        "stale_session_epoch",
        "session_not_active",
        "session_lost",
        "session_limit_reached",
        "statement_not_cancellable",
        "session_mailbox_full",
    }
)

_BROKER_ERROR_MESSAGES: dict[str, str] = {
    "stale_session_epoch": "Session was taken over by another window",
    "session_not_active": "Session is not accepting new statements",
    "session_lost": "Session is lost and must be re-attached",
    "session_limit_reached": "Too many active console sessions",
    "statement_not_cancellable": "Statement cannot be cancelled in its current state",
    "session_mailbox_full": "Session queue is full",
}

# 语句结果可读的状态(设计 §2.2):cancelled/timeout **保留已落 spool 的部分行**,
# 与 job 路径"取消即删 spool"不同,是刻意偏离(§11-7)。
_RESULT_READABLE_STATES = frozenset(
    {
        ConsoleStatementState.ACCEPTED,
        ConsoleStatementState.EXECUTING,
        ConsoleStatementState.STREAMING,
        ConsoleStatementState.SUCCEEDED,
        ConsoleStatementState.CANCELLED,
        ConsoleStatementState.TIMEOUT,
    }
)

_TERMINAL_STATEMENT_STATES = frozenset(
    {
        ConsoleStatementState.SUCCEEDED,
        ConsoleStatementState.FAILED,
        ConsoleStatementState.CANCELLED,
        ConsoleStatementState.TIMEOUT,
        ConsoleStatementState.OUTCOME_UNKNOWN,
        ConsoleStatementState.SKIPPED,
    }
)

# 可导出的语句状态:终态且**有结果**。cancelled/timeout 在列 —— 保留的部分行
# 可读即可导出(设计 §2.2 / F6);failed/outcome_unknown/skipped 不在列。
_EXPORTABLE_STATEMENT_STATES = frozenset(
    {
        ConsoleStatementState.SUCCEEDED,
        ConsoleStatementState.CANCELLED,
        ConsoleStatementState.TIMEOUT,
    }
)


@router.post("/sql/sessions/attach", response_model=SessionResponse)
def attach_session(body: SessionAttachRequest, request: Request) -> SessionResponse:
    """绑定或接管 console 的会话;无活会话则新建(异步建连)。

    已有活会话时**不重建连接**,只 bump epoch 并返回现状(设计 §2.3 M2/M3)。
    """

    services = services_from(request)
    user = current_user_from(request)
    broker = _require_broker(services)
    console = _console_for_current_user(request, body.console_id)
    datasource_id = console["datasource_id"]
    if datasource_id is None:
        raise ApiError(400, "console_datasource_required", "SQL console has no datasource bound")
    datasource = _datasource_for_current_user(request, str(datasource_id))
    db_type = DbType(str(datasource["db_type"]))
    if db_type not in SESSION_CAPABLE_DB_TYPES:
        raise ApiError(
            409,
            "console_session_unsupported",
            "This datasource dialect has no interactive session",
        )

    session = _translate(
        lambda: broker.attach(
            AttachRequest(
                console_id=body.console_id,
                datasource_id=str(datasource_id),
                owner_user_id=user.id,
            )
        )
    )
    _audit_session(
        services,
        request,
        user_id=user.id,
        project_id=str(datasource["project_id"]),
        action="console_session_attach",
        resource_id=session.id,
        detail={"console_id": body.console_id, "epoch": session.epoch},
    )
    observation = _translate(lambda: broker.observe(session.id))
    return _session_response(observation, db_type)


@router.get("/sql/sessions/{session_id}", response_model=SessionResponse)
def observe_session(session_id: str, request: Request) -> SessionResponse:
    """observe 会话。观察类请求不设 epoch 栅栏,但响应恒带 current_epoch(§2.3)。"""

    services = services_from(request)
    broker = _require_broker(services)
    observation = _translate(lambda: broker.observe(session_id))
    _require_session_owner(request, observation.session)
    datasource = _datasource_for_current_user(request, observation.session.datasource_id)
    return _session_response(observation, DbType(str(datasource["db_type"])))


@router.post(
    "/sql/sessions/{session_id}/statements",
    response_model=StatementSubmitResponse,
    status_code=202,
)
def submit_statement(
    session_id: str,
    body: StatementSubmitRequest,
    request: Request,
) -> StatementSubmitResponse:
    """提交语句(202)。同 `client_request_id` 重发只回原语句行,永不重执行(§3.1)。

    `page_size` / `max_result_rows` 是**结果集成形参数**:前者决定驱动 fetchmany
    与 spool 落盘的批大小,后者是客户端截断上限(取够就停,连接与会话续用)。
    两者作为生效快照随结果集落库,`GET .../result` 原样回读(设计 §3.2)。
    """

    services = services_from(request)
    broker = _require_broker(services)
    observation = _translate(lambda: broker.observe(session_id))
    _require_session_owner(request, observation.session)
    try:
        sql = validate_readonly_sql(body.sql)
    except SqlGuardError as exc:
        # R5:只报拒绝原因,不回显 SQL 原文。
        logger.info(
            "sql guard rejected console statement",
            request_id=request_id_from(request),
            reason=str(exc),
        )
        raise ApiError(400, "invalid_sql", "Only read-only SELECT/WITH SQL is allowed") from exc

    kwargs: dict[str, Any] = {
        "page_size": body.page_size,
        "max_result_rows": body.max_result_rows,
    }
    if body.timeout_seconds is not None:
        kwargs["timeout_seconds"] = body.timeout_seconds
    receipt = _translate(
        lambda: broker.submit(
            session_id,
            body.epoch,
            sql,
            body.client_request_id,
            **kwargs,
        )
    )
    return StatementSubmitResponse(
        statement_id=receipt.statement.id,
        result_set_id=receipt.statement.result_set_id,
        seq=receipt.statement.seq,
        deduplicated=receipt.deduplicated,
    )


@router.get("/sql/statements/{statement_id}/progress", response_model=StatementProgressResponse)
def get_statement_progress(
    statement_id: str,
    request: Request,
    after_version: int = Query(default=0, ge=0),
) -> StatementProgressResponse:
    """语句进度(与 `JobProgressResponse` 字段镜像,设计 §3.2)。

    内嵌 `session` 块 ⇒ 执行期间一条轮询同时驱动语句与会话状态渲染。
    """

    services = services_from(request)
    broker = _require_broker(services)
    statement = _statement_for_current_user(request, broker, statement_id)
    observation = _translate(lambda: broker.observe(statement.session_id))

    terminal = statement.state in _TERMINAL_STATEMENT_STATES
    manifest = (
        _spool_manifest_or_none(services, statement.result_set_id)
        if statement.result_set_id is not None
        else None
    )
    result_set_row = (
        _result_set_row(services, statement.result_set_id)
        if statement.result_set_id is not None
        else None
    )
    loaded_rows = _int_from_manifest(manifest, "loaded_rows") or 0
    result_version = _int_from_manifest(manifest, "result_version") or 0
    columns_ready = bool(_columns_from_manifest(manifest))
    first_batch_at = _manifest_datetime(manifest, "first_batch_at")
    has_new_result = result_version > after_version
    return StatementProgressResponse(
        statement_id=statement.id,
        session=StatementSessionBlock(
            session_id=observation.session.id,
            state=observation.state,
            current_epoch=observation.current_epoch,
        ),
        result_set_id=statement.result_set_id,
        state=statement.state,
        loaded_rows=loaded_rows,
        result_version=result_version,
        columns_ready=columns_ready,
        first_batch_ready=first_batch_at is not None or (terminal and columns_ready),
        terminal=terminal,
        error=statement.error_summary,
        error_code=statement.error_code,
        retry_after_ms=poll_retry_after_ms(
            terminal=terminal,
            fresh=has_new_result,
            queued=statement.state is ConsoleStatementState.ACCEPTED,
        ),
        has_new_result=has_new_result,
        truncated=_bool_from_manifest(manifest, "truncated"),
        has_more=_bool_from_manifest(manifest, "has_more"),
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
        timings=_statement_stage_timings(statement, result_set_row),
        execution=_statement_execution_metrics(statement, result_set_row, manifest),
    )


@router.get("/sql/statements/{statement_id}/result", response_model=StatementResultResponse)
def get_statement_result(
    statement_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> StatementResultResponse:
    """读结果页(读 spool,与 `GET /jobs/{id}/result` 同形,limit≤1000)。"""

    services = services_from(request)
    broker = _require_broker(services)
    statement = _statement_for_current_user(request, broker, statement_id)
    if statement.state not in _RESULT_READABLE_STATES:
        raise ApiError(409, "statement_not_successful", "Statement result is not ready")
    result_set_id = statement.result_set_id
    if result_set_id is None:
        raise ApiError(404, "not_found", "Statement result not found")

    rows = services.result_store.fetch_range(result_set_id, offset, limit)
    preview_rows, preview_truncated_cells = _preview_result_rows(rows)
    manifest = _spool_manifest_or_none(services, result_set_id)
    result_set_row = _result_set_row(services, result_set_id)
    execution = _result_set_metadata_section(result_set_row, "execution")
    loaded_rows = _int_from_manifest(manifest, "loaded_rows")
    state = str(result_set_row["state"]) if result_set_row is not None else None
    total_rows = _row_int(result_set_row, "total_rows")
    truncated = _bool_from_manifest(manifest, "truncated")
    has_more = bool(
        state == "streaming"
        or truncated
        or _bool_from_manifest(manifest, "has_more")
        or (loaded_rows is not None and offset + len(rows) < loaded_rows)
    )
    return StatementResultResponse(
        statement_id=statement.id,
        statement_state=statement.state,
        result_set_id=result_set_id,
        offset=offset,
        limit=limit,
        columns=_columns_from_manifest(manifest),
        rows=[RowResponse(values=row.values) for row in preview_rows],
        loaded_rows=loaded_rows,
        total_rows=total_rows,
        state=state,
        truncated=truncated,
        has_more=has_more,
        # job 路径从 job payload 取这两个值;语句侧的来源是 submit 时落在
        # result_sets.storage_ref.metadata 里的生效快照(设计 §3.2)。
        page_size=_dict_int(execution, "page_size"),
        max_result_rows=_dict_int(execution, "max_rows"),
        preview_truncated_cells=preview_truncated_cells,
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
    )


@router.post(
    "/sql/statements/{statement_id}/export",
    response_model=ExportCreateResponse,
    status_code=202,
)
def create_statement_export(
    statement_id: str,
    body: ExportCreateRequest,
    request: Request,
) -> ExportCreateResponse:
    """导出语句结果(设计 §3.3"结果导出"平价)。

    **复用导出模块与一次性 token,不另造一套**:落的仍是 `RESULT_EXPORT` job,
    worker 侧读 `source_result_set_id` 的 spool —— 会话与 job 共用同一个
    result catalog,所以 worker 一行不用改;下载走既有 `/api/exports/{token}`。

    可导出状态含 `cancelled` / `timeout`:取消保留的部分结果**可读即可导出**
    (设计 §2.2 / F6),这正是与 job 路径"取消即删 spool"的刻意差别。
    """

    services = services_from(request)
    user = current_user_from(request)
    broker = _require_broker(services)
    statement = _statement_for_current_user(request, broker, statement_id)
    if statement.state not in _EXPORTABLE_STATEMENT_STATES:
        raise ApiError(
            409,
            "statement_not_successful",
            "Statement must reach a terminal state with results before export",
        )
    result_set_id = statement.result_set_id
    if result_set_id is None or _spool_manifest_or_none(services, result_set_id) is None:
        raise ApiError(404, "not_found", "Statement result not found")
    datasource = _datasource_for_current_user(request, statement.datasource_id)
    _enforce_export_rate_limit(services, user.id)

    export_job_id = new_id()
    filename = _export_filename(statement_id, body.format)
    content_type = export_content_type(body.format)
    download_token = token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=services.download_url_ttl_seconds)
    job = Job(
        id=export_job_id,
        kind=JobKind.RESULT_EXPORT,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(datasource["project_id"]),
        datasource_ids=[statement.datasource_id],
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={
            # job 导出用 source_job_id;语句导出给 source_statement_id ——
            # worker 只认 source_result_set_id,这一列纯粹是证据链锚点。
            "source_statement_id": statement_id,
            "source_result_set_id": result_set_id,
            "format": body.format,
            "filename": filename,
            "table_name": body.table_name,
            "source_db_type": str(datasource["db_type"]),
        },
    )
    services.job_backend.enqueue(job)
    with services.engine.begin() as conn:
        conn.execute(
            insert(export_download_tokens).values(
                token_hash=_download_token_hash(download_token),
                job_id=export_job_id,
                owner_user_id=user.id,
                format=body.format,
                filename=filename,
                content_type=content_type,
                expires_at=expires_at,
            )
        )
    _audit_session(
        services,
        request,
        user_id=user.id,
        project_id=str(datasource["project_id"]),
        action="console_statement_export",
        resource_id=export_job_id,
        detail={"statement_id": statement_id, "format": body.format},
    )
    return ExportCreateResponse(
        job_id=export_job_id,
        download_token=download_token,
        expires_at=expires_at,
        format=body.format,
        filename=filename,
    )


@router.post("/sql/statements/{statement_id}/cancel", response_model=StatementCancelResponse)
def cancel_statement(
    statement_id: str,
    body: StatementCancelRequest,
    request: Request,
) -> StatementCancelResponse:
    """取消(排队中=出队;执行中=控制通道硬取消,设计 §4.1 阶梯)。

    取消权随 epoch 移交(M8):旧 tab 的 cancel 得 409,不允许"看不见结果的窗口
    杀掉别人正观察的查询"。
    """

    services = services_from(request)
    broker = _require_broker(services)
    statement = _statement_for_current_user(request, broker, statement_id)
    receipt = _translate(lambda: broker.cancel(statement_id, body.epoch))
    _audit_session(
        services,
        request,
        user_id=statement.owner_user_id,
        project_id=None,
        action="console_statement_cancel",
        resource_id=statement_id,
        detail={"accepted": receipt.accepted, "statement_state": receipt.statement_state.value},
    )
    return StatementCancelResponse(
        accepted=receipt.accepted,
        statement_state=receipt.statement_state,
    )


@router.post("/sql/sessions/{session_id}/close", response_model=SessionResponse)
def close_session(
    session_id: str,
    body: SessionCloseRequest,
    request: Request,
) -> SessionResponse:
    """优雅关闭(先取消在跑语句 → rollback → 断连)。

    close 在 lane mailbox 上排队仲裁(M6),故响应给的是**受理时**的观察值:
    状态可能仍是 executing/cancelling,由前端继续 observe 收敛到 closed。
    """

    services = services_from(request)
    broker = _require_broker(services)
    observation = _translate(lambda: broker.observe(session_id))
    _require_session_owner(request, observation.session)
    datasource = _datasource_for_current_user(request, observation.session.datasource_id)
    _translate(lambda: broker.close(session_id, body.epoch))
    _audit_session(
        services,
        request,
        user_id=observation.session.owner_user_id,
        project_id=str(datasource["project_id"]),
        action="console_session_close",
        resource_id=session_id,
        detail={"console_id": observation.session.console_id},
    )
    after = _translate(lambda: broker.observe(session_id))
    return _session_response(after, DbType(str(datasource["db_type"])))


# ── helpers ──────────────────────────────────────────────────────────────────


def _require_broker(services: ApiServices) -> SessionBroker:
    """没装配 broker = 回退开关关闭(设计 §6)。如实 409,前端整体走 job 路径。"""

    broker = getattr(services, "session_broker", None)
    if not isinstance(broker, SessionBroker):
        raise ApiError(
            409,
            "console_session_disabled",
            "Console sessions are disabled on this deployment",
        )
    return broker


def _translate[T](call: Callable[[], T]) -> T:
    """`BrokerError` → HTTP。未登记的 code 宁可 500 也不静默降级成 200。"""

    try:
        return call()
    except BrokerError as exc:
        if exc.code == "statement_not_found":
            raise ApiError(404, "not_found", "Statement not found") from exc
        if exc.code in _BROKER_CONFLICT_CODES:
            raise ApiError(
                409,
                exc.code,
                _BROKER_ERROR_MESSAGES[exc.code],
                extra=(
                    {"current_epoch": exc.current_epoch} if exc.current_epoch is not None else None
                ),
            ) from exc
        raise


def _require_session_owner(request: Request, session: ConsoleSession) -> None:
    """会话属主 = console 属主(设计 §5.1)。非属主一律 404,不泄漏会话存在性。"""

    user = current_user_from(request)
    if session.owner_user_id != user.id:
        raise ApiError(404, "not_found", "Console session not found")


def _statement_for_current_user(
    request: Request,
    broker: SessionBroker,
    statement_id: str,
) -> ConsoleStatement:
    statement = _translate(lambda: broker.statement(statement_id))
    user = current_user_from(request)
    if statement.owner_user_id != user.id:
        raise ApiError(404, "not_found", "Statement not found")
    return statement


def _session_response(observation: SessionObservation, db_type: DbType) -> SessionResponse:
    session = observation.session
    return SessionResponse(
        session_id=session.id,
        epoch=session.epoch,
        current_epoch=observation.current_epoch,
        state=observation.state,
        db_type=db_type,
        server_cancel=session.server_cancel,
        current_statement_id=observation.current_statement_id,
        idle_deadline=observation.idle_deadline,
        last_activity_at=session.last_activity_at,
        close_reason=session.close_reason,
        error_code=session.error_code,
    )


def _row_int(row: RowMapping | None, key: str) -> int | None:
    if row is None:
        return None
    value = row[key]
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _statement_stage_timings(
    statement: ConsoleStatement,
    result_set_row: RowMapping | None,
) -> JobStageTimings | None:
    """`JobStageTimings` 镜像。

    `queue_ms` = submit → lane 取出(会话的"排队"是 mailbox,不是 job 队列);
    `connect_ms` **恒为 None** —— 会话复用已建连接,没有 per-statement 建连;
    在这里填 0 会让"免握手收益"的性能对比读出假数据(设计 §9 性能冒烟)。
    """

    raw = _result_set_metadata_section(result_set_row, "timings")
    timings: dict[str, int | None] = {
        "queue_ms": _datetime_delta_ms(statement.submitted_at, statement.started_at),
        "connect_ms": None,
        "execute_first_row_ms": _dict_int(raw, "execute_first_row_ms"),
        "fetch_ms": _dict_int(raw, "fetch_ms"),
        "spool_ms": _dict_int(raw, "spool_ms"),
        "total_ms": _datetime_delta_ms(statement.submitted_at, statement.finished_at),
    }
    if not any(value is not None for value in timings.values()):
        return None
    return JobStageTimings(**timings)


def _statement_execution_metrics(
    statement: ConsoleStatement,
    result_set_row: RowMapping | None,
    manifest: dict[str, Any] | None,
) -> JobExecutionMetrics:
    """`JobExecutionMetrics` 镜像。会话路径没有的量留 None,不编默认值。"""

    execution = _result_set_metadata_section(result_set_row, "execution")
    timings = _statement_stage_timings(statement, result_set_row)
    loaded_rows = _int_from_manifest(manifest, "loaded_rows")
    return JobExecutionMetrics(
        queued_at=statement.submitted_at,
        claimed_at=statement.started_at,
        # 会话连接在 attach 时就建好了:语句维度没有 connect 阶段。
        connect_started_at=None,
        connected_at=None,
        execute_started_at=_dict_datetime(execution, "execute_started_at"),
        first_row_at=_dict_datetime(execution, "first_row_at"),
        first_batch_at=(
            _manifest_datetime(manifest, "first_batch_at")
            or _dict_datetime(execution, "first_batch_at")
        ),
        finished_reading_at=_dict_datetime(execution, "finished_reading_at"),
        finished_at=statement.finished_at,
        queue_ms=timings.queue_ms if timings else None,
        connect_ms=None,
        execute_to_first_row_ms=timings.execute_first_row_ms if timings else None,
        fetch_ms=timings.fetch_ms if timings else None,
        spool_ms=timings.spool_ms if timings else None,
        total_ms=timings.total_ms if timings else None,
        rows_read=_dict_int(execution, "rows_read"),
        rows_returned=_dict_int(execution, "rows_returned", fallback=loaded_rows),
        max_rows=_dict_int(execution, "max_rows"),
        limit_pushdown=_dict_bool(execution, "limit_pushdown"),
        limit_pushdown_reason=_dict_str(execution, "limit_pushdown_reason"),
        output_limit_applied=_dict_bool(execution, "output_limit_applied"),
        query_shape=_dict_str(execution, "query_shape"),
        effective_sql_hash=_dict_str(execution, "effective_sql_hash"),
        db_type=_dict_str(execution, "db_type"),
        # 语句不跑在 worker 上,它跑在 API 进程的 lane 线程上(设计 D1)。
        worker_id=None,
    )


def _audit_session(
    services: ApiServices,
    request: Request,
    *,
    user_id: str,
    project_id: str | None,
    action: str,
    resource_id: str,
    detail: dict[str, object],
) -> None:
    services.write_audit(
        user_id=user_id,
        project_id=project_id,
        action=action,
        resource_type="console_session",
        resource_id=resource_id,
        result="accepted",
        request_id=request_id_from(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )
