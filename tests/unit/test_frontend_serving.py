"""API 进程伺服前端 SPA dist 的挂载语义(2.0 前后端同机同进程,零反代)。

覆盖:
- 未配置 DATAOPS_FRONTEND_DIST → 不挂载,API-only 行为不变(healthz / api 404)。
- 配置后:SPA fallback 命中(前端深链 /projects/... 回 index.html)。
- /api/* 不存在的路径仍是 API 404 JSON,不被 fallback 吞。
- /healthz 不受影响。
- 资产带长 cache 头,index.html 不缓存。
- 前端路由公开:无 token 也能拿到 index.html(不被鉴权拦)。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.config import ApiConfig, Settings
from tests._asgi_client import AsgiClient

_INDEX_HTML = (
    "<!doctype html><html><head><title>DataOps</title></head><body><div id=app></div></body></html>"
)
_ASSET_JS = "console.log('dataops-bundle');"


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    """最小可挂载 dist:index.html + assets 指纹文件 + 顶层 favicon。"""

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-a1b2c3d4.js").write_text(_ASSET_JS, encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return dist


def _settings_with_dist(dist: Path | None) -> Settings:
    settings = Settings()
    settings.api = ApiConfig()
    settings.api.frontend_dist = dist
    return settings


def _app(dist: Path | None) -> AsgiClient:
    services = _FakeServices()
    app = create_app(
        services=cast(ApiServices, services),
        settings=_settings_with_dist(dist),
    )
    return AsgiClient(app)


# --- 未配置:行为与 API-only 现状一致 ---------------------------------------


def test_no_dist_does_not_mount_spa_fallback() -> None:
    client = _app(None)

    # 非 api / 非 healthz GET 没有 SPA 兜底 → 不是 200 的 index.html。
    response = client.get("/projects/123")

    assert response.status_code != 200
    assert b"<div id=app>" not in response.body


def test_no_dist_healthz_unaffected() -> None:
    client = _app(None)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_dist_directory_falls_back_to_api_only(tmp_path: Path) -> None:
    """配置了路径但目录不存在 → 不挂载(不炸 API)。"""

    client = _app(tmp_path / "does-not-exist")

    assert client.get("/healthz").status_code == 200
    assert client.get("/projects/123").status_code != 200


def test_dist_without_index_falls_back_to_api_only(tmp_path: Path) -> None:
    empty = tmp_path / "empty-dist"
    empty.mkdir()

    client = _app(empty)

    assert client.get("/healthz").status_code == 200
    assert client.get("/projects/123").status_code != 200


# --- 已配置:挂载语义 -------------------------------------------------------


def test_spa_fallback_serves_index_for_frontend_route(dist_dir: Path) -> None:
    client = _app(dist_dir)

    response = client.get("/projects/123")

    assert response.status_code == 200
    assert b"<div id=app>" in response.body
    # index.html 不缓存。
    assert response.headers.get("cache-control") == "no-cache"


def test_spa_fallback_public_without_token(dist_dir: Path) -> None:
    """前端路由是公开 GET 内容,无 token 不应被鉴权拦成 401。"""

    client = _app(dist_dir)

    response = client.get("/admin/users")

    assert response.status_code == 200
    assert b"<div id=app>" in response.body


def test_root_serves_index(dist_dir: Path) -> None:
    client = _app(dist_dir)

    response = client.get("/")

    assert response.status_code == 200
    assert b"<div id=app>" in response.body


def test_assets_served_with_long_cache(dist_dir: Path) -> None:
    client = _app(dist_dir)

    response = client.get("/assets/index-a1b2c3d4.js")

    assert response.status_code == 200
    assert b"dataops-bundle" in response.body
    assert response.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_real_top_level_file_served(dist_dir: Path) -> None:
    client = _app(dist_dir)

    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.body.startswith(b"\x00\x00\x01\x00")


def test_api_404_not_swallowed_by_fallback(dist_dir: Path) -> None:
    """/api/* 不存在路径仍是 API 404 JSON,不被 SPA index.html 吞。

    带合法 token(过鉴权)后,未知 /api 路径必须是 404,而非 SPA index.html。
    """

    client = _app(dist_dir)
    token = create_access_token(user_id="user-1", role="admin", secret="jwt-secret")

    response = client.get(
        "/api/does/not/exist",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert b"<div id=app>" not in response.body


def test_healthz_unaffected_when_serving_frontend(dist_dir: Path) -> None:
    client = _app(dist_dir)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_route_still_requires_auth_when_serving_frontend(dist_dir: Path) -> None:
    """伺服前端不削弱 /api/* 鉴权:未带 token 的受保护 API 仍 401。"""

    client = _app(dist_dir)

    response = client.get("/api/projects")

    assert response.status_code == 401


# --- fakes -----------------------------------------------------------------


class _RateLimiter:
    def allow(self, key: str) -> bool:
        del key
        return True


class _FakeServices:
    def __init__(self) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self):  # type: ignore[no-untyped-def]
        from app.domain.license import LicenseMode

        return LicenseMode.TRIAL

    def is_token_revoked(self, **kwargs: object) -> bool:
        del kwargs
        return False

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)
