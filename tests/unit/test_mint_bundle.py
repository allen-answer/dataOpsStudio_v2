from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.package import build_mint_bundle as build


def test_start_script_retries_initialization_and_drops_admin_password() -> None:
    start = (build.SCRIPTS_ROOT / "start.sh").read_text(encoding="utf-8")

    assert ".bundle-initialized" in start
    assert "--update-password" in start
    assert "unset DATAOPS_ADMIN_PASSWORD" in start
    assert start.index("unset DATAOPS_ADMIN_PASSWORD") < start.index("invoke_launcher up")


def test_cache_marker_requires_the_current_pin(tmp_path: Path) -> None:
    marker = tmp_path / "source.sha256"
    marker.write_text("old\n", encoding="ascii")

    assert not build.cache_marker_matches(marker, "current")
    marker.write_text("current\n", encoding="ascii")
    assert build.cache_marker_matches(marker, "current")


def test_wheel_cache_is_bound_to_current_requirements(tmp_path: Path, monkeypatch: Any) -> None:
    cache = tmp_path / "cache"
    wheels = cache / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "stale.whl").write_bytes(b"stale")
    (wheels / ".complete").write_text("old\n", encoding="ascii")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        del cwd, env
        calls.append(command)
        (wheels / "current.whl").write_bytes(b"current")

    monkeypatch.setattr(build, "run", fake_run)
    result = build.prepare_wheels(cache, tmp_path / "python", requirements)

    assert calls
    assert result == wheels
    assert not (wheels / "stale.whl").exists()
    assert (wheels / ".complete").read_text(encoding="ascii").strip() == build.sha256_file(
        requirements
    )
