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
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError

# spool manifest 读取与 console/datasource 授权是两条路径共用的深模块;
# 复制一份等于给"两套轮询契约漂移"开门(设计 §3.2),故直接复用 core 的实现。
from app.api.routes.core import (
    _bool_from_manifest,
    _columns_from_manifest,
    _console_for_current_user,
    _datasource_for_current_user,
    _int_from_manifest,
    _manifest_datetime,
    _manifest_optional_str,
    _manifest_pagination_mode,
    _preview_result_rows,
    _result_set_state,
    _result_set_total_rows,
    _spool_manifest_or_none,
)
from app.api.schemas import (
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
from app.api.services import ApiServices
from app.broker import BrokerError, SessionBroker, SessionObservation
from app.broker.store import AttachRequest
from app.broker.wiring import SESSION_CAPABLE_DB_TYPES
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.console_session import ConsoleSession, ConsoleStatement, ConsoleStatementState
from app.domain.datasource import DbType

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

    `page_size` / `max_result_rows` 是**结果集成形参数**,在 lane 落 spool 时生效;
    A4 的 lane 还不落 spool(spool 平价是 A5),此处只做契约校验。
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

    kwargs: dict[str, Any] = {}
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
        retry_after_ms=_retry_after_ms(statement.state, terminal=terminal, fresh=has_new_result),
        has_new_result=has_new_result,
        truncated=_bool_from_manifest(manifest, "truncated"),
        has_more=_bool_from_manifest(manifest, "has_more"),
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
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
    loaded_rows = _int_from_manifest(manifest, "loaded_rows")
    state = _result_set_state(services, result_set_id)
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
        total_rows=_result_set_total_rows(services, result_set_id),
        state=state,
        truncated=truncated,
        has_more=has_more,
        # job 路径从 job payload 取这两个值;语句侧的来源(console_statements 快照)
        # 随 spool 平价一起接线(A5),在此之前如实回 null 而不是编一个默认值。
        page_size=None,
        max_result_rows=None,
        preview_truncated_cells=preview_truncated_cells,
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
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


def _retry_after_ms(state: ConsoleStatementState, *, terminal: bool, fresh: bool) -> int:
    """轮询调速沿用 job 路径 0/1000/2000 规则;排队态给 500ms(设计 §3.2)。"""

    if terminal:
        return 0
    if state is ConsoleStatementState.ACCEPTED:
        return 500
    return 1000 if fresh else 2000


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
