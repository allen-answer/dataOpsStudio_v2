from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

_SAFE_BUILD_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")


def _installed_version() -> str:
    try:
        return package_version("dataops-studio")
    except PackageNotFoundError:
        return "unknown"


def _safe_env(name: str, fallback: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        return fallback
    if _SAFE_BUILD_VALUE.fullmatch(value) is None:
        return "unknown"
    return value


def build_info() -> dict[str, str]:
    """Return non-secret build identity shared by API and worker images."""

    version = _safe_env("DATAOPS_BUILD_VERSION", _installed_version())
    return {
        "version": version,
        "commit": _safe_env("DATAOPS_BUILD_COMMIT", "unknown"),
        "image_version": _safe_env("DATAOPS_IMAGE_VERSION", version),
    }
