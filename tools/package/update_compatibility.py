"""Compatibility identity for payload-only desktop updates.

Payload updates may replace only ``app/`` and ``frontend/dist/``.  Everything
that must remain fixed is represented here so an update built for a different
Python/wheel/PostgreSQL/Tauri/startup/migration/updater baseline fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IGNORED_SOURCE_DIRS = {"__pycache__", "gen", "target"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _artifact(name: str, value: Mapping[str, str]) -> dict[str, str]:
    if set(value) != {"version", "sha256"}:
        raise ValueError(f"{name} identity must contain version and sha256")
    version = value["version"].strip()
    sha256 = value["sha256"].strip().lower()
    if _SAFE_VALUE.fullmatch(version) is None:
        raise ValueError(f"invalid {name} version")
    if _SHA256.fullmatch(sha256) is None:
        raise ValueError(f"invalid {name} sha256")
    return {"version": version, "sha256": sha256}


def _source_fingerprint(repo_root: Path, *relative_paths: str) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        source = repo_root / relative
        if not source.exists():
            raise FileNotFoundError(f"compatibility source is missing: {relative}")
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in candidates:
            if _IGNORED_SOURCE_DIRS.intersection(path.parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError(f"compatibility source must not be a symlink: {path}")
            if not path.is_file():
                continue
            name = path.relative_to(repo_root).as_posix().encode("utf-8")
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _runtime_project_fingerprint(repo_root: Path) -> str:
    project_file = repo_root / "pyproject.toml"
    python_file = repo_root / ".python-version"
    project = tomllib.loads(project_file.read_text(encoding="utf-8")).get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    value = {
        "python_version": python_file.read_text(encoding="ascii").strip(),
        "requires_python": project.get("requires-python"),
        "dependencies": project.get("dependencies"),
        "optional_dependencies": project.get("optional-dependencies"),
    }
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def build_update_compatibility(
    *,
    repo_root: Path,
    platform: str,
    requirements: Path,
    python: Mapping[str, str],
    uv: Mapping[str, str],
    postgres: Mapping[str, str],
) -> dict[str, object]:
    """Return the exact baseline a payload update must match."""

    normalized_platform = platform.strip()
    if _SAFE_VALUE.fullmatch(normalized_platform) is None:
        raise ValueError("invalid update platform")
    compatibility: dict[str, object] = {
        "schema_version": 1,
        "platform": normalized_platform,
        "python": _artifact("python", python),
        "uv": _artifact("uv", uv),
        "postgres": _artifact("postgres", postgres),
        "requirements_sha256": sha256_file(requirements),
        "source_fingerprints": {
            "desktop": _source_fingerprint(repo_root, "desktop/src-tauri"),
            "migrations": _source_fingerprint(repo_root, "alembic.ini", "app/db/migrations"),
            "packaging": _source_fingerprint(repo_root, "tools/package"),
            "runtime_project": _runtime_project_fingerprint(repo_root),
            "runtime_scripts": _source_fingerprint(repo_root, "bundle-scripts"),
            "updater": _source_fingerprint(
                repo_root,
                "updater",
                "app/infrastructure/license/signatures.py",
            ),
        },
    }
    compatibility["compatibility_id"] = hashlib.sha256(_canonical_json(compatibility)).hexdigest()
    return compatibility


def write_update_metadata(
    *,
    runtime_dir: Path,
    compatibility: Mapping[str, object],
    version: str,
    commit: str,
    image_version: str,
) -> None:
    """Write the full-package trust baseline and its immutable build identity."""

    runtime_dir.mkdir(parents=True, exist_ok=True)
    identity = {"version": version, "commit": commit, "image_version": image_version}
    (runtime_dir / "update-compatibility.json").write_text(
        json.dumps(compatibility, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "build-identity.json").write_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = ["build_update_compatibility", "sha256_file", "write_update_metadata"]
