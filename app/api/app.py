from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from app.api.errors import ApiError, api_error_handler, unhandled_error_handler
from app.api.middleware.crosscutting import CrossCuttingMiddleware
from app.api.routes import router
from app.api.services import ApiServices, build_api_services
from app.config import Settings

ExceptionHandler = Callable[[Request, Exception], Response | Awaitable[Response]]


def create_app(
    *,
    services: ApiServices | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    actual_services = services or build_api_services(settings)
    app = FastAPI(title="DataOps Studio API", version="2.0.0a0")
    app.state.services = actual_services
    app.add_exception_handler(ApiError, cast(ExceptionHandler, api_error_handler))
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(CrossCuttingMiddleware, services=actual_services)
    app.include_router(router, prefix="/api")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
