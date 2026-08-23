from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tools.package import build_win10_bundle as build

PS_SCRIPTS = ("_common.ps1", "install.ps1", "start.ps1", "stop.ps1")
ALL_SCRIPTS = (*PS_SCRIPTS, "start.cmd", "stop.cmd")


def test_launcher_invoked_via_call_operator_without_batch_quotes(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    common = (tmp_path / "_common.ps1").read_text(encoding="ascii")
    # The launcher is invoked with the call operator and single-variable paths -
    # no nested-quote string to get wrong (the batch quote-hell this replaces).
    assert (
        "& $VenvPy -m app.launcher --root $env:DATAOPS_HOME --pg-bin-dir $PgBin --uv $UvExe @args"
    ) in common

    for name in PS_SCRIPTS:
        text = (tmp_path / name).read_text(encoding="ascii")
        assert 'set "' not in text  # no batch `set "VAR=..."` quoting survives
        assert "setlocal" not in text


def test_start_script_delegates_install_then_first_run_then_up(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    start = (tmp_path / "start.ps1").read_text(encoding="ascii")
    assert "install.ps1" in start  # shared provisioning path, both modes
    assert "Invoke-Launcher bootstrap init" in start
    assert "Invoke-Launcher up" in start


def test_first_run_is_retryable_and_drops_admin_password_before_up(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    common = (tmp_path / "_common.ps1").read_text(encoding="ascii")
    start = (tmp_path / "start.ps1").read_text(encoding="ascii")

    assert "$($args -join ' ')" not in common
    assert ".bundle-initialized" in start
    assert "--update-password" in start
    assert "Remove-Item Env:DATAOPS_ADMIN_PASSWORD" in start
    assert start.index("Remove-Item Env:DATAOPS_ADMIN_PASSWORD") < start.index("Invoke-Launcher up")


def test_install_script_covers_offline_and_online(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    install = (tmp_path / "install.ps1").read_text(encoding="ascii")
    # offline branch: bundled wheels, no network
    assert "--no-index" in install
    assert "--find-links" in install
    # online branch: manifest-driven download with sha256 verification
    assert "download-manifest.json" in install
    assert "Get-FileHash" in install
    # both branches pin hashes for the dependency install
    assert "--require-hashes" in install


def test_cmd_shims_are_one_line_powershell_launchers(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    start_cmd = (tmp_path / "start.cmd").read_text(encoding="ascii")
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*' in start_cmd
    stop_cmd = (tmp_path / "stop.cmd").read_text(encoding="ascii")
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*' in stop_cmd


def test_no_legacy_bat_scripts_emitted(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    assert not (tmp_path / "start.bat").exists()
    assert not (tmp_path / "stop.bat").exists()


def test_scripts_are_windows_ascii_crlf(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    for name in ALL_SCRIPTS:
        data = (tmp_path / name).read_bytes()
        data.decode("ascii")
        assert b"\r\n" in data


def test_download_manifest_pins_url_and_sha256_for_each_artifact(tmp_path: Path) -> None:
    build.write_download_manifest(tmp_path)

    manifest = json.loads(
        (tmp_path / "runtime" / "download-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["mode"] == "online"
    for key in ("python", "uv", "postgres"):
        assert manifest[key]["url"].startswith("https://")
        assert len(manifest[key]["sha256"]) == 64
    assert manifest["postgres"]["keep"] == ["bin", "lib", "share"]


def test_extract_pg_keeps_only_server_runtime_and_license(tmp_path: Path) -> None:
    pg_zip = tmp_path / "pg.zip"
    with zipfile.ZipFile(pg_zip, "w") as archive:
        archive.writestr("pgsql/bin/postgres.exe", b"postgres")
        archive.writestr("pgsql/lib/libpq.dll", b"libpq")
        archive.writestr("pgsql/share/postgresql.conf.sample", b"config")
        archive.writestr("pgsql/doc/readme.txt", b"drop")
        archive.writestr("pgsql/include/libpq-fe.h", b"drop")
        archive.writestr("pgsql/server_license.txt", b"license")

    staging = tmp_path / "staging"
    build.extract_pg(pg_zip, staging)

    assert (staging / "pgsql" / "bin" / "postgres.exe").read_bytes() == b"postgres"
    assert (staging / "pgsql" / "lib" / "libpq.dll").read_bytes() == b"libpq"
    assert (staging / "pgsql" / "share" / "postgresql.conf.sample").read_bytes() == b"config"
    assert (staging / "pgsql" / "server_license.txt").read_bytes() == b"license"
    assert not (staging / "pgsql" / "doc").exists()
    assert not (staging / "pgsql" / "include").exists()


def test_wheel_cache_is_bound_to_current_requirements(tmp_path: Path, monkeypatch: object) -> None:
    cache = tmp_path / "cache"
    wheels = cache / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "stale.whl").write_bytes(b"stale")
    (cache / "wheels.requirements.sha256").write_text("old\n", encoding="ascii")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path | None = None) -> None:
        del cwd
        calls.append(command)
        (wheels / "current.whl").write_bytes(b"current")

    monkeypatch.setattr(build, "run", fake_run)  # type: ignore[attr-defined]
    result = build.download_wheels(cache, tmp_path / "python", requirements)

    assert calls
    assert result == wheels
    assert not (wheels / "stale.whl").exists()
    assert (cache / "wheels.requirements.sha256").read_text(encoding="ascii").strip() == (
        build.sha256_file(requirements)
    )
