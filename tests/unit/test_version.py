from __future__ import annotations

import pytest

from app.version import build_info


def test_build_info_uses_build_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAOPS_BUILD_VERSION", "2.0.1-ci")
    monkeypatch.setenv("DATAOPS_BUILD_COMMIT", "0123456789abcdef")
    monkeypatch.setenv("DATAOPS_IMAGE_VERSION", "2.0.1-test")

    assert build_info() == {
        "version": "2.0.1-ci",
        "commit": "0123456789abcdef",
        "image_version": "2.0.1-test",
    }


def test_build_info_rejects_unsafe_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAOPS_BUILD_COMMIT", "bad\nvalue")

    assert build_info()["commit"] == "unknown"
