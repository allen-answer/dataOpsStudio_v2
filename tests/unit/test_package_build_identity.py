from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.package.build_identity import (
    BuildIdentity,
    inject_build_identity,
    render_powershell_build_identity,
    render_shell_build_identity,
    resolve_build_identity,
)


def _source_archive(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "dataops-studio"\nversion = "2.0.1"\n',
        encoding="utf-8",
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_source(root: Path) -> str:
    _source_archive(root)
    _git(root, "init")
    _git(root, "add", "pyproject.toml")
    _git(
        root,
        "-c",
        "user.name=Build Identity Test",
        "-c",
        "user.email=build-identity@example.invalid",
        "commit",
        "-m",
        "test source",
    )
    return _git(root, "rev-parse", "HEAD")


def test_explicit_archive_identity_renders_safe_shells(tmp_path: Path) -> None:
    _source_archive(tmp_path)
    identity = resolve_build_identity(
        tmp_path,
        explicit_commit="a" * 40,
        image_version="2.0.1-mint21.3-amd64-offline",
    )

    assert identity == BuildIdentity(
        version="2.0.1",
        commit="a" * 40,
        image_version="2.0.1-mint21.3-amd64-offline",
    )
    assert render_shell_build_identity(identity).splitlines() == [
        "export DATAOPS_BUILD_VERSION=2.0.1",
        f"export DATAOPS_BUILD_COMMIT={'a' * 40}",
        "export DATAOPS_IMAGE_VERSION=2.0.1-mint21.3-amd64-offline",
    ]
    assert render_powershell_build_identity(identity).splitlines() == [
        "$env:DATAOPS_BUILD_VERSION = '2.0.1'",
        "$env:DATAOPS_BUILD_COMMIT = '" + "a" * 40 + "'",
        "$env:DATAOPS_IMAGE_VERSION = '2.0.1-mint21.3-amd64-offline'",
    ]


def test_source_archive_without_explicit_commit_fails_closed(tmp_path: Path) -> None:
    _source_archive(tmp_path)

    with pytest.raises(SystemExit, match="--build-commit"):
        resolve_build_identity(
            tmp_path,
            explicit_commit=None,
            image_version="2.0.1-mint21.3-amd64-offline",
        )


def test_nested_source_archive_does_not_borrow_parent_git_identity(tmp_path: Path) -> None:
    _git_source(tmp_path)
    archive = tmp_path / "exported-source"
    archive.mkdir()
    _source_archive(archive)

    with pytest.raises(SystemExit, match="--build-commit"):
        resolve_build_identity(
            archive,
            explicit_commit=None,
            image_version="2.0.1-mint21.3-amd64-offline",
        )


def test_clean_git_worktree_uses_head_and_rejects_mismatch(tmp_path: Path) -> None:
    head = _git_source(tmp_path)

    identity = resolve_build_identity(
        tmp_path,
        explicit_commit=None,
        image_version="2.0.1-mint21.3-amd64-offline",
    )
    assert identity.commit == head

    with pytest.raises(SystemExit, match="does not match Git HEAD"):
        resolve_build_identity(
            tmp_path,
            explicit_commit="a" * 40,
            image_version="2.0.1-mint21.3-amd64-offline",
        )


@pytest.mark.parametrize("dirty_name", ["pyproject.toml", "untracked.txt"])
def test_git_worktree_must_be_clean(dirty_name: str, tmp_path: Path) -> None:
    _git_source(tmp_path)
    path = tmp_path / dirty_name
    path.write_text(path.read_text(encoding="utf-8") + "\n" if path.exists() else "dirty\n")

    with pytest.raises(SystemExit, match="dirty Git worktree"):
        resolve_build_identity(
            tmp_path,
            explicit_commit=None,
            image_version="2.0.1-mint21.3-amd64-offline",
        )


@pytest.mark.parametrize(
    "commit",
    ["short", "g" * 40, "a" * 39, "a" * 65, "a" * 40 + "\nunsafe"],
)
def test_build_identity_rejects_invalid_commit(commit: str, tmp_path: Path) -> None:
    _source_archive(tmp_path)

    with pytest.raises(SystemExit, match="commit"):
        resolve_build_identity(
            tmp_path,
            explicit_commit=commit,
            image_version="2.0.1-mint21.3-amd64-offline",
        )


def test_build_identity_rejects_unsafe_image_version(tmp_path: Path) -> None:
    _source_archive(tmp_path)

    with pytest.raises(SystemExit, match="image version"):
        resolve_build_identity(
            tmp_path,
            explicit_commit="a" * 40,
            image_version="2.0.1\nunsafe",
        )


def test_identity_injection_fails_on_ambiguous_marker() -> None:
    with pytest.raises(SystemExit, match="marker"):
        inject_build_identity(
            "# marker\n# marker\n",
            marker="# marker",
            rendered_identity="export DATAOPS_BUILD_VERSION=2.0.1",
            keep_shebang=False,
        )
