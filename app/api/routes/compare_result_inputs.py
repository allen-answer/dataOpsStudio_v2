from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.schemas import CompareResultInputCreateRequest
from app.domain.compare_result_input import (
    ActorProject,
    CompareResultInputDescriptor,
    JobResultOrigin,
    StatementResultOrigin,
)
from app.infrastructure.compare_result_inputs import (
    CompareResultInputCaptureFailed,
    CompareResultInputDeleted,
    CompareResultInputError,
    CompareResultInputExpired,
    CompareResultInputLost,
    CompareResultInputNotFound,
    CompareResultInputs,
    ResultGone,
    ResultNotComparable,
    ResultNotTerminal,
    ResultOriginNotFound,
)

router = APIRouter()


@router.post(
    "/projects/{project_id}/compare/result-inputs",
    response_model=CompareResultInputDescriptor,
    status_code=201,
)
def capture_compare_result_input(
    project_id: str,
    body: CompareResultInputCreateRequest,
    request: Request,
) -> CompareResultInputDescriptor:
    services = services_from(request)
    user = current_user_from(request)
    module = _module(request)
    origin = (
        StatementResultOrigin(body.origin_id)
        if body.origin_kind == "statement"
        else JobResultOrigin(body.origin_id)
    )
    try:
        descriptor = module.capture(
            origin,
            scope=ActorProject(actor_id=user.id, project_id=project_id),
        )
    except CompareResultInputError as exc:
        raise _api_error(exc) from exc
    services.write_audit(
        user_id=user.id,
        project_id=project_id,
        action="compare_result_input_capture",
        resource_type="compare_result_input",
        resource_id=descriptor.id,
        result="success",
        request_id=request_id_from(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        detail={
            "origin_kind": descriptor.origin_kind,
            "loaded_rows": descriptor.loaded_rows,
            "truncated": descriptor.truncated,
        },
    )
    return descriptor


@router.get(
    "/projects/{project_id}/compare/result-inputs/{input_id}",
    response_model=CompareResultInputDescriptor,
)
def get_compare_result_input(
    project_id: str,
    input_id: str,
    request: Request,
) -> CompareResultInputDescriptor:
    user = current_user_from(request)
    try:
        opened = _module(request).open(
            input_id,
            scope=ActorProject(actor_id=user.id, project_id=project_id),
            batch_size=1,
            retain_until=datetime.now(UTC) + timedelta(minutes=5),
        )
    except CompareResultInputError as exc:
        raise _api_error(exc) from exc
    return opened.descriptor


def _module(request: Request) -> CompareResultInputs:
    services = services_from(request)
    return CompareResultInputs(
        services.engine,
        services.result_store,
        ttl_days=services.result_ttl_days,
    )


def _api_error(exc: CompareResultInputError) -> ApiError:
    if isinstance(exc, (ResultOriginNotFound, CompareResultInputNotFound)):
        return ApiError(404, exc.code, "Result input not found")
    if isinstance(exc, (ResultNotTerminal, ResultNotComparable)):
        return ApiError(409, exc.code, "Result is not ready for comparison")
    if isinstance(
        exc,
        (ResultGone, CompareResultInputExpired, CompareResultInputDeleted, CompareResultInputLost),
    ):
        return ApiError(410, exc.code, "Result input is no longer available")
    if isinstance(exc, CompareResultInputCaptureFailed):
        return ApiError(503, exc.code, "Unable to snapshot result input")
    return ApiError(500, exc.code, "Compare result input failed")


__all__ = ["router"]
