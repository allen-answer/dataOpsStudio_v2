from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    internal_detail: str | None = None


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
