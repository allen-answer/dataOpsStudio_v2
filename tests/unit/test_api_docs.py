from __future__ import annotations

from typing import cast

from app.api.app import create_app
from app.api.services import ApiServices
from app.config import ApiConfig, Form, Settings


def test_api_docs_enabled_by_default_in_dev() -> None:
    app = create_app(
        services=cast(ApiServices, _NoopServices()),
        settings=Settings(form=Form.DEV, api=ApiConfig(enable_docs=None)),
    )

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_api_docs_disabled_by_default_outside_dev() -> None:
    app = create_app(
        services=cast(ApiServices, _NoopServices()),
        settings=Settings(form=Form.HOSTED, api=ApiConfig(enable_docs=None)),
    )

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_api_docs_can_be_explicitly_enabled_outside_dev() -> None:
    app = create_app(
        services=cast(ApiServices, _NoopServices()),
        settings=Settings(form=Form.HOSTED, api=ApiConfig(enable_docs=True)),
    )

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


class _NoopServices:
    pass
