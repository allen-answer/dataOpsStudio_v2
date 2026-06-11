"""前端 SPA 静态产物伺服(2.0 前后端同机同进程,零反代)。

人已拍板:前后端都在云服务器跑、暂不要反代。最简正解 = FastAPI 直接挂载
`frontend/dist` 静态产物 + SPA 路由 fallback,API 进程一并伺服前端。

挂载语义(见 create_app 接线):
- ``/api/*`` / ``/healthz`` 由既有路由优先处理,**不受本模块影响**。
- ``/assets/*`` 等指纹文件从 dist 伺服,长 cache(Vite 文件名带 content hash,
  ``immutable`` 安全)。
- ``index.html`` 不缓存(``no-cache``),保证发版后客户端立即取新壳。
- SPA fallback:非 ``/api``、非真实文件的 GET 路径回 ``index.html``,
  前端路由(``/projects/...`` / ``/admin/...``)直链 / 刷新可用。
- ``/api/*`` 不存在的路径仍是 API 404 JSON,**不被 fallback 吞掉**
  (本模块的 catch-all 显式排除 ``/api`` 前缀)。

未配置 ``DATAOPS_FRONTEND_DIST`` → 不调用本模块,API-only 行为与现状一致。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

_INDEX_FILENAME = "index.html"
_ASSETS_DIRNAME = "assets"

# Vite 资产文件名带内容指纹(如 index-a1b2c3d4.js),内容变 → 文件名变,
# 故可放心长 cache + immutable。
_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# index.html 文件名固定,发版后内容会变,必须每次回源校验。
_INDEX_CACHE_CONTROL = "no-cache"


def resolve_frontend_dist(dist: Path | None) -> Path | None:
    """把配置值解析成可挂载的 dist 目录,无效则返回 None(调用方据此跳过挂载)。

    要求:目录存在,且含 ``index.html``。任一不满足 → None,
    create_app 退回 API-only,不抛错(部署期 dist 还没 build 也不应炸 API)。
    """

    if dist is None:
        return None
    dist_dir = dist.expanduser()
    index_file = dist_dir / _INDEX_FILENAME
    if not dist_dir.is_dir() or not index_file.is_file():
        return None
    return dist_dir


def mount_frontend(app: FastAPI, dist_dir: Path) -> None:
    """把 SPA dist 挂到 app:``/assets`` 静态 + SPA catch-all fallback。

    调用前提:``resolve_frontend_dist`` 已确认 dist_dir 有效(目录在 + index 在)。
    必须在 ``include_router(prefix="/api")`` 之后调用,使 ``/api/*`` 路由优先匹配。
    """

    index_file = dist_dir / _INDEX_FILENAME
    assets_dir = dist_dir / _ASSETS_DIRNAME

    if assets_dir.is_dir():
        app.mount(
            f"/{_ASSETS_DIRNAME}",
            _CachedStaticFiles(directory=assets_dir),
            name="frontend-assets",
        )

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa_fallback(spa_path: str, request: Request) -> Response:
        # /api/* 不存在的路径绝不被 SPA 吞掉:回 API 风格 404 JSON。
        # (正常 /api/* 已被前面注册的 router 命中,不会走到这里。)
        if spa_path == "api" or spa_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "message": "Not Found"},
            )

        # 命中 dist 里的真实文件(favicon.ico / robots.txt / 顶层 .js 等)→ 直接伺服。
        candidate = _safe_join(dist_dir, spa_path)
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)

        # 其余 GET 路径 = 前端路由 → 回 index.html(不缓存)。
        return FileResponse(index_file, headers={"Cache-Control": _INDEX_CACHE_CONTROL})


class _CachedStaticFiles(StaticFiles):
    """StaticFiles + 给指纹资产打长 cache 头。"""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = _ASSET_CACHE_CONTROL
        return response


def _safe_join(root: Path, relative: str) -> Path | None:
    """把请求路径安全地拼到 dist 根下,挡 ``..`` 越界。返回 None 表示越界 / 非法。"""

    if not relative:
        return None
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate == root_resolved or root_resolved in candidate.parents:
        return candidate
    return None
