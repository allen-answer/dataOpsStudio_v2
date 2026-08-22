from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


@dataclass
class ApiError(Exception):
    """API 业务错误。

    ★ 不能 frozen:异常穿越 @contextmanager(如 sqlalchemy engine.begin())时,
    contextlib 以 Python 赋值方式设置 exc.__traceback__,frozen dataclass 会抛
    FrozenInstanceError,把任意 4xx 变成 500(2026-06-11 真机实锤;
    回归测试:tests/unit/test_api_errors.py)。
    """

    status_code: int
    code: str
    message: str
    internal_detail: str | None = None
    # 随错误体一起返回的**非敏感**上下文键(如 409 stale_session_epoch 的
    # current_epoch,设计 §2.3 M1)。None = 载荷形态与既有错误完全一致。
    extra: dict[str, object] | None = None


def error_payload(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_payload(code, message))


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    if exc.internal_detail:
        logger.info(
            "api error",
            request_id=_request_id(request),
            code=exc.code,
            detail=exc.internal_detail,
        )
    if exc.extra:
        payload: dict[str, object] = {**error_payload(exc.code, exc.message), **exc.extra}
        return JSONResponse(status_code=exc.status_code, content=payload)
    return error_response(exc.status_code, exc.code, exc.message)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled api error",
        request_id=_request_id(request),
        path=request.url.path,
    )
    return error_response(500, "internal_error", "Request failed")


def safe_detail(value: Any) -> str:
    return str(value) if value is not None else ""


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None
