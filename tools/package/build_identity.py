"""Validated build identity shared by the Windows and Mint package builders."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    version: str
    commit: str
    image_version: str


def _safe_value(name: str, value: str) -> str:
    normalized = value.strip()
    if _SAFE_VALUE.fullmatch(normalized) is None:
        raise SystemExit(f"invalid {name}")
    return normalized


def _project_version(repo_root: Path) -> str:
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    try:
        project = text.split("[project]", maxsplit=1)[1].split("[", maxsplit=1)[0]
    except IndexError as exc:
        raise SystemExit("cannot read [project] version") from exc
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if match is None:
        raise SystemExit("cannot read [project] version")
    return _safe_value("project version", match.group(1))


def _run_git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SystemExit("cannot verify package source Git state") from exc
    return result.stdout.strip()


def _resolve_commit(repo_root: Path, explicit_commit: str | None) -> str:
    has_git_metadata = (repo_root / ".git").exists()
    if has_git_metadata:
        top_level = Path(_run_git(repo_root, "rev-parse", "--show-toplevel")).resolve()
        if top_level != repo_root.resolve():
            raise SystemExit("package source is not the Git worktree root")
        head = _run_git(repo_root, "rev-parse", "--verify", "HEAD").lower()
        status = _run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise SystemExit("refusing to package a dirty Git worktree")
        if explicit_commit is not None and explicit_commit.strip().lower() != head:
            raise SystemExit("explicit build commit does not match Git HEAD")
        commit = head
    elif explicit_commit is not None:
        commit = explicit_commit.strip().lower()
    else:
        raise SystemExit(
            "cannot resolve build commit from this source archive; pass --build-commit"
        )
    if _GIT_COMMIT.fullmatch(commit) is None:
        raise SystemExit("invalid build commit; expected a full 40- or 64-character Git object id")
    return commit


def resolve_build_identity(
    repo_root: Path,
    *,
    explicit_commit: str | None,
    image_version: str,
) -> BuildIdentity:
    return BuildIdentity(
        version=_project_version(repo_root),
        commit=_resolve_commit(repo_root, explicit_commit),
        image_version=_safe_value("image version", image_version),
    )


def render_shell_build_identity(identity: BuildIdentity) -> str:
    return "\n".join(
        (
            f"export DATAOPS_BUILD_VERSION={identity.version}",
            f"export DATAOPS_BUILD_COMMIT={identity.commit}",
            f"export DATAOPS_IMAGE_VERSION={identity.image_version}",
        )
    )


def render_powershell_build_identity(identity: BuildIdentity) -> str:
    return "\n".join(
        (
            f"$env:DATAOPS_BUILD_VERSION = '{identity.version}'",
            f"$env:DATAOPS_BUILD_COMMIT = '{identity.commit}'",
            f"$env:DATAOPS_IMAGE_VERSION = '{identity.image_version}'",
        )
    )


def inject_build_identity(
    template: str,
    *,
    marker: str,
    rendered_identity: str,
    keep_shebang: bool,
) -> str:
    if template.count(marker) != 1:
        raise SystemExit("build identity marker is missing or duplicated")
    injected = template.replace(marker, rendered_identity)
    compacted: list[str] = []
    for index, line in enumerate(injected.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        is_shebang = keep_shebang and index == 0 and stripped.startswith("#!")
        if stripped.startswith("#") and not is_shebang:
            continue
        compacted.append(line)
    return "\n".join(compacted) + "\n"


__all__ = [
    "BuildIdentity",
    "inject_build_identity",
    "render_powershell_build_identity",
    "render_shell_build_identity",
    "resolve_build_identity",
]
