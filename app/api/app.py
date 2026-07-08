from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from app.api.errors import ApiError, api_error_handler, unhandled_error_handler
from app.api.frontend import mount_frontend, resolve_frontend_dist
from app.api.middleware.crosscutting import CrossCuttingMiddleware
from app.api.routes import router
from app.api.services import ApiServices, build_api_services
from app.config import Form, Settings
from app.services.workflow_scheduler import WorkflowScheduler

ExceptionHandler = Callable[[Request, Exception], Response | Awaitable[Response]]


def create_app(
    *,
    services: ApiServices | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    actual_services = services or build_api_services(settings)
    expose_docs = _docs_enabled(settings)
    frontend_dist = resolve_frontend_dist(
        settings.api.frontend_dist if settings is not None else None
    )
    scheduler = _build_scheduler(actual_services, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # cron tick 后台线程随 API 进程起停(ADR-0009 §1:进程内调度,无独立进程)。
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(
        title="DataOps Studio API",
        version="2.0.0a0",
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
        lifespan=lifespan,
    )
    app.state.services = actual_services
    app.add_exception_handler(ApiError, cast(ExceptionHandler, api_error_handler))
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(
        CrossCuttingMiddleware,
        services=actual_services,
        serve_frontend=frontend_dist is not None,
    )
    app.include_router(router, prefix="/api")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ★ 前端伺服必须在 /api 路由与 /healthz 之后挂载,使其优先匹配,
    # SPA catch-all 只兜住剩余 GET 路径(见 app.api.frontend 模块文档)。
    if frontend_dist is not None:
        mount_frontend(app, frontend_dist)

    return app


def _build_scheduler(services: ApiServices, settings: Settings | None) -> WorkflowScheduler | None:
    # 仅在真实进程(main.py 传 settings)且开关开时启动;测试用 services= 建 app
    # (settings=None)不起后台线程,行为与现状一致。
    if settings is None or not settings.scheduler.enabled:
        return None
    return WorkflowScheduler(
        services,
        tick_interval_seconds=settings.scheduler.tick_interval_seconds,
    )


def _docs_enabled(settings: Settings | None) -> bool:
    if settings is None:
        return False
    if settings.api.enable_docs is not None:
        return settings.api.enable_docs
    return settings.form is Form.DEV
