from __future__ import annotations

import os
import socket
import stat
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.launcher import (
    LauncherContext,
    LauncherError,
    _child_env,
    _pg_ctl_start,
    _sync_license_state,
    _which,
    command_bootstrap_init,
    command_doctor,
    command_pg_up,
)


def test_bootstrap_init_generates_0600_files(tmp_path: Path) -> None:
    context = _context(tmp_path)

    assert command_bootstrap_init(context, Namespace(force=False)) == 0

    bootstrap = LocalFileBootstrapSecrets.from_config_dir(tmp_path / "config")
    assert bootstrap.get_master_key()
    assert bootstrap.get_pg_app_password()
    assert bootstrap.get_pg_superuser_password()
    assert bootstrap.get_jwt_secret()
    if os.name != "nt":
        for path in (
            bootstrap.master_key_file,
            bootstrap.pg_app_password_file,
            bootstrap.pg_superuser_password_file,
            bootstrap.jwt_secret_file,
        ):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bootstrap_init_reports_missing_license_trial_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _context(tmp_path)

    assert command_bootstrap_init(context, Namespace(force=False)) == 0

    output = capsys.readouterr().out
    assert "license.lic not found" in output
    assert "30-day trial mode" in output


def test_child_env_removes_legacy_postgres_dev_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_DEV_PASSWORD", "legacy")
    context = _context(tmp_path)

    env = _child_env(context)

    assert "POSTGRES_DEV_PASSWORD" not in env
    assert env["DATAOPS_BOOTSTRAP_MASTER_KEY_FILE"] == str(
        context.settings.bootstrap.master_key_file
    )
    assert env["DATAOPS_RESULT_LOCAL_ROOT"] == str(context.settings.result_store.local_root)
    assert env["DATAOPS_WORKER_RESULT_GC_INTERVAL_SECONDS"] == str(
        context.settings.worker.result_gc_interval_seconds
    )


def test_pg_up_does_not_initdb_when_pgdata_already_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    command_bootstrap_init(context, Namespace(force=False))
    context.pgdata_dir.mkdir(parents=True)
    (context.pgdata_dir / "PG_VERSION").write_text("16\n", encoding="utf-8")
    calls: list[str] = []

    def fail_initdb(unused_context: LauncherContext) -> None:
        del unused_context
        raise AssertionError("initdb should not run for existing pgdata")

    monkeypatch.setattr("app.launcher._initdb", fail_initdb)
    monkeypatch.setattr("app.launcher._pg_ctl_start", lambda unused_context: calls.append("start"))
    monkeypatch.setattr("app.launcher._wait_for_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.launcher._ensure_metadata_database", lambda *args: calls.append("ensure")
    )
    monkeypatch.setattr("app.launcher._database_ready", lambda *args, **kwargs: True)

    assert command_pg_up(context, Namespace()) == 0

    assert calls == ["ensure"]


def test_sync_license_state_preserves_existing_trial_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    command_bootstrap_init(context, Namespace(force=False))
    existing_expires_at = datetime(2026, 7, 1, tzinfo=UTC)
    engine = _LicenseStateEngine(
        {
            "id": 1,
            "mode": "trial",
            "expires_at": existing_expires_at,
        }
    )
    monkeypatch.setattr("app.launcher._metadata_engine", lambda *args, **kwargs: engine)

    _sync_license_state(context, LocalFileBootstrapSecrets.from_config_dir(tmp_path / "config"))

    assert engine.updated_values is not None
    assert engine.operation == "update"
    assert engine.updated_values["expires_at"] == existing_expires_at


def test_sync_license_state_updates_existing_license_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    command_bootstrap_init(context, Namespace(force=False))
    engine = _LicenseStateEngine(
        {
            "id": 1,
            "mode": "valid",
            "expires_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    monkeypatch.setattr("app.launcher._metadata_engine", lambda *args, **kwargs: engine)

    _sync_license_state(context, LocalFileBootstrapSecrets.from_config_dir(tmp_path / "config"))

    assert engine.operation == "update"
    assert engine.updated_values is not None
    assert engine.updated_values["id"] == 1


def test_pg_ctl_start_uses_runtime_socket_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "app.launcher._find_pg_binary",
        lambda unused_context, name, required=True: Path(f"/pg/bin/{name}"),
    )
    monkeypatch.setattr(
        "app.launcher._run",
        lambda command, *, cwd, env: commands.append(command),
    )

    _pg_ctl_start(context)

    assert commands
    options = commands[0][commands[0].index("-o") + 1]
    assert f"-k {context.data_dir / 'pg-socket'}" in options


def test_which_falls_back_to_common_uv_install_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv_path = tmp_path / ".local" / "bin" / "uv"
    uv_path.parent.mkdir(parents=True)
    uv_path.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("app.launcher.shutil.which", lambda command: None)
    monkeypatch.setattr("app.launcher.Path.home", lambda: tmp_path)

    assert _which("uv") == uv_path


def test_doctor_reports_pg_port_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _context(tmp_path)
    context.settings.db.host = "127.0.0.1"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        context.settings.db.port = int(listener.getsockname()[1])
        monkeypatch.setattr("app.launcher._which", lambda command: Path("/bin/uv"))
        monkeypatch.setattr(
            "app.launcher._find_pg_binary",
            lambda unused_context, name, required=True: Path(f"/pg/bin/{name}"),
        )

        assert command_doctor(context, Namespace()) == 1

    output = capsys.readouterr().out
    assert "PG TCP port free" in output
    assert "DATAOPS_PG_PORT" in output


def test_pg_ctl_start_prints_pg_log_tail_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _context(tmp_path)
    context.log_dir.mkdir(parents=True)
    (context.log_dir / "pg.log").write_text(
        "line1\nline2\nline3\nline4\nline5\nline6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.launcher._find_pg_binary",
        lambda unused_context, name, required=True: Path(f"/pg/bin/{name}"),
    )

    def fail_run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        del command, cwd, env
        raise LauncherError("pg_ctl failed")

    monkeypatch.setattr("app.launcher._run", fail_run)

    with pytest.raises(LauncherError):
        _pg_ctl_start(context)

    stderr = capsys.readouterr().err
    assert "last 5 lines" in stderr
    assert "line2" in stderr
    assert "line6" in stderr


def _context(root: Path) -> LauncherContext:
    settings = Settings()
    settings.bootstrap.master_key_file = root / "config" / ".secret_master.key"
    settings.bootstrap.pg_app_password_file = root / "config" / "secrets" / "pg_app_password"
    settings.bootstrap.pg_superuser_password_file = (
        root / "config" / "secrets" / "pg_superuser_password"
    )
    settings.bootstrap.jwt_secret_file = root / "config" / "secrets" / "jwt_secret"
    settings.bootstrap.license_file = root / "config" / "license.lic"
    settings.result_store.local_root = root / "data" / "results"
    return LauncherContext(root=root, repo_root=Path.cwd(), settings=settings, uv="uv")


class _LicenseStateEngine:
    def __init__(self, existing: dict[str, Any] | None) -> None:
        self.existing = existing
        self.updated_values: dict[str, Any] | None = None
        self.operation: str | None = None

    def begin(self) -> _LicenseStateConnection:
        return _LicenseStateConnection(self)

    def dispose(self) -> None:
        return None


class _LicenseStateConnection:
    def __init__(self, engine: _LicenseStateEngine) -> None:
        self.engine = engine
        self.calls = 0

    def __enter__(self) -> _LicenseStateConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: Any) -> _LicenseStateResult:
        self.calls += 1
        if self.calls == 1:
            selected = {column.name for column in statement.selected_columns}
            assert {"id", "mode", "expires_at"}.issubset(selected)
            return _LicenseStateResult(self.engine.existing)
        compiled = statement.compile()
        self.engine.operation = statement.__visit_name__
        self.engine.updated_values = dict(compiled.params)
        return _LicenseStateResult(None)


class _LicenseStateResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def mappings(self) -> _LicenseStateResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self.row
