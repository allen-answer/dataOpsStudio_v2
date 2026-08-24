from __future__ import annotations

import argparse
import base64
import getpass
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import URL, create_engine, insert, select, text, update
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import license_state, project_members, projects, users
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.license import verify_license
from app.infrastructure.secretstore.local_file import LocalFileSecretStore

SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700
DEFAULT_PG_SUPERUSER = "postgres"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class LauncherError(RuntimeError):
    """User-facing launcher error. Messages must not include secret values."""


@dataclass(frozen=True)
class LauncherContext:
    root: Path
    repo_root: Path
    settings: Settings
    uv: str
    pg_bin_dir: Path | None = None

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def pgdata_dir(self) -> Path:
        return self.data_dir / "pgdata"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


LauncherCommand = Callable[[LauncherContext, argparse.Namespace], int]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = cast(LauncherCommand | None, getattr(args, "func", None))
    if func is None:
        parser.print_help()
        return 2

    try:
        context = _context_from_args(args)
        return func(context, args)
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def command_doctor(context: LauncherContext, args: argparse.Namespace) -> int:
    del args
    ok = True
    ok &= _doctor_check(
        "Python 3.12.x",
        sys.version_info.major == 3 and sys.version_info.minor == 12,
        f"current={sys.version.split()[0]}",
    )
    uv_path = _which(context.uv)
    ok &= _doctor_check(
        "uv",
        uv_path is not None,
        str(uv_path) if uv_path is not None else _uv_missing_detail(context.uv),
    )

    for binary_name in ("initdb", "pg_ctl", "postgres"):
        binary = _find_pg_binary(context, binary_name, required=False)
        ok &= _doctor_check(
            f"PostgreSQL {binary_name}",
            binary is not None,
            str(binary or "missing"),
        )

    _ensure_runtime_dirs(context)
    ok &= _doctor_check(
        "config dir writable",
        os.access(context.config_dir, os.W_OK),
        str(context.config_dir),
    )
    ok &= _doctor_check(
        "data dir writable",
        os.access(context.data_dir, os.W_OK),
        str(context.data_dir),
    )
    ok &= _doctor_check(
        "log dir writable",
        os.access(context.log_dir, os.W_OK),
        str(context.log_dir),
    )
    pg_port_free = _tcp_port_available(context.settings.db.host, context.settings.db.port)
    ok &= _doctor_check(
        "PG TCP port free",
        pg_port_free,
        f"{_connect_host(context.settings.db.host)}:{context.settings.db.port}"
        if pg_port_free
        else (
            f"{_connect_host(context.settings.db.host)}:{context.settings.db.port} is already "
            "accepting TCP connections; set DATAOPS_PG_PORT to a free port"
        ),
    )
    return 0 if ok else 1


def command_bootstrap_init(context: LauncherContext, args: argparse.Namespace) -> int:
    _ensure_runtime_dirs(context)
    force = bool(args.force)
    generated = [
        _write_secret_file(
            context.settings.bootstrap.master_key_file,
            base64.urlsafe_b64encode(secrets.token_bytes(32)) + b"\n",
            force=force,
        ),
        _write_secret_file(
            context.settings.bootstrap.pg_app_password_file,
            f"{secrets.token_urlsafe(48)}\n".encode(),
            force=force,
        ),
        _write_secret_file(
            context.settings.bootstrap.pg_superuser_password_file,
            f"{secrets.token_urlsafe(48)}\n".encode(),
            force=force,
        ),
        _write_secret_file(
            context.settings.bootstrap.jwt_secret_file,
            f"{secrets.token_urlsafe(48)}\n".encode(),
            force=force,
        ),
    ]
    created = sum(1 for item in generated if item)
    kept = len(generated) - created
    print(f"bootstrap secrets ready: created={created}, kept={kept}")
    if _is_windows():
        print("Windows portable: chmod skipped; Windows ACL hardening is post-2.0.0.")
    else:
        print("Bootstrap secret files are chmod 0600.")
    _print_license_hint(context)
    return 0


def command_pg_up(context: LauncherContext, args: argparse.Namespace) -> int:
    del args
    _ensure_runtime_dirs(context)
    bootstrap = _bootstrap(context)
    if not (context.pgdata_dir / "PG_VERSION").exists():
        _initdb(context)
    else:
        print(f"PG data dir already initialized: {context.pgdata_dir}")

    super_password = bootstrap.get_pg_superuser_password()
    if _database_ready(
        context,
        user=DEFAULT_PG_SUPERUSER,
        password=super_password,
        database="postgres",
        timeout_seconds=1.0,
    ):
        print("PG already running.")
    else:
        _pg_ctl_start(context)
        _wait_for_database(
            context,
            user=DEFAULT_PG_SUPERUSER,
            password=super_password,
            database="postgres",
            timeout_seconds=30.0,
        )

    _ensure_metadata_database(context, bootstrap)
    print(f"PG ready at {context.settings.db.host}:{context.settings.db.port}")
    return 0


def command_pg_stop(context: LauncherContext, args: argparse.Namespace) -> int:
    force = bool(getattr(args, "force", False))
    if not (context.pgdata_dir / "PG_VERSION").exists():
        print(f"PG data dir is not initialized: {context.pgdata_dir}")
        return 0
    _pg_ctl_stop(context, force=force)
    return 0


def command_pg_status(context: LauncherContext, args: argparse.Namespace) -> int:
    del args
    if not context.settings.bootstrap.pg_superuser_password_file.exists():
        print("PG status: bootstrap secrets missing")
        return 1
    bootstrap = _bootstrap(context)
    ready = _database_ready(
        context,
        user=DEFAULT_PG_SUPERUSER,
        password=bootstrap.get_pg_superuser_password(),
        database="postgres",
        timeout_seconds=1.0,
    )
    print("PG status: ready" if ready else "PG status: stopped")
    return 0 if ready else 1


def command_alembic_up(context: LauncherContext, args: argparse.Namespace) -> int:
    del args
    bootstrap = _bootstrap(context)
    env = _child_env(context)
    env["PGPASSWORD"] = bootstrap.get_pg_app_password()
    env["DATAOPS_DATABASE_URL"] = _metadata_database_url(
        context,
        user=context.settings.db.user,
        password=None,
        database=context.settings.db.database,
    ).render_as_string(hide_password=False)
    _run([context.uv, "run", "alembic", "upgrade", "head"], cwd=context.repo_root, env=env)
    _sync_license_state(context, bootstrap)
    print("alembic upgrade head complete")
    return 0


def command_admin_create(context: LauncherContext, args: argparse.Namespace) -> int:
    username = str(args.username)
    # ast-grep-ignore: r2-no-plaintext-password-access
    password = str(args.password) if args.password else getpass.getpass("Admin password: ")
    if not password:
        raise LauncherError("admin password must not be empty")
    role = str(args.role)
    project_name = str(args.project_name)
    # ast-grep-ignore: r2-no-plaintext-prefixed-credential-attribute-access
    update_password = bool(args.update_password)

    bootstrap = _bootstrap(context)
    engine = _metadata_engine(
        context,
        user=context.settings.db.user,
        password=bootstrap.get_pg_app_password(),
        database=context.settings.db.database,
    )
    try:
        secret_store = LocalFileSecretStore(engine, bootstrap)
        password_hash = secret_store.hash_password(password).ref
        with engine.begin() as conn:
            existing = (
                conn.execute(select(users.c.id).where(users.c.username == username))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if not update_password:
                    raise LauncherError(
                        "admin user already exists; use --update-password to rotate it"
                    )
                user_id = str(existing["id"])
                conn.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(password_hash=password_hash, role=role)
                )
                print(f"admin user updated: username={username}, user_id={user_id}")
                return 0

            user_id = str(uuid4())
            project_id = str(uuid4())
            conn.execute(
                insert(users).values(
                    id=user_id,
                    username=username,
                    password_hash=password_hash,
                    role=role,
                )
            )
            conn.execute(
                insert(projects).values(
                    id=project_id,
                    name=project_name,
                    owner_user_id=user_id,
                    description="Created by dataops launcher.",
                )
            )
            conn.execute(
                insert(project_members).values(
                    project_id=project_id,
                    user_id=user_id,
                    role="owner",
                )
            )
    finally:
        engine.dispose()

    print(f"admin user created: username={username}, user_id={user_id}, project_id={project_id}")
    return 0


def command_up(context: LauncherContext, args: argparse.Namespace) -> int:
    stop_request = context.data_dir / "launcher.stop" if _is_windows() else None
    if stop_request is not None:
        _write_pid(context.data_dir / "launcher.pid", os.getpid())
    try:
        command_pg_up(context, args)
        if _finish_windows_startup_stop(context, stop_request):
            return 0
        command_alembic_up(context, args)
        if _finish_windows_startup_stop(context, stop_request):
            return 0
    except BaseException:
        if stop_request is not None:
            _unlink_if_exists(context.data_dir / "launcher.pid")
            _unlink_if_exists(stop_request)
        raise
    if stop_request is None:
        _write_pid(context.data_dir / "launcher.pid", os.getpid())
    api = _start_child(context, "api", [sys.executable, "-m", "app.main"])
    worker = _start_child(context, "worker", [sys.executable, "-m", "app.worker"])
    children = {"api": api, "worker": worker}
    stop_requested = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal stop_requested
        del signum, frame
        stop_requested = True

    previous_term = signal.signal(signal.SIGTERM, handle_signal)
    previous_int = signal.signal(signal.SIGINT, handle_signal)
    try:
        print(
            "DataOpsStudio is running. Press Ctrl+C to stop gracefully; "
            "large active queries may take time to drain."
        )
        while not stop_requested:
            if stop_request is not None and stop_request.is_file():
                stop_requested = True
                break
            for name, process in children.items():
                return_code = process.poll()
                if return_code is not None:
                    print(f"{name} exited unexpectedly with code {return_code}", file=sys.stderr)
                    _stop_children(children, force=True, grace_seconds=5)
                    command_pg_stop(context, argparse.Namespace(force=True))
                    return 1
            time.sleep(1.0)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)

    _stop_children(children, force=False, grace_seconds=float(args.grace_seconds))
    _unlink_child_pid_files(context)
    command_pg_stop(context, argparse.Namespace(force=False))
    _unlink_if_exists(context.data_dir / "launcher.pid")
    if stop_request is not None:
        _unlink_if_exists(stop_request)
    return 0


def _finish_windows_startup_stop(
    context: LauncherContext,
    stop_request: Path | None,
) -> bool:
    if stop_request is None or not stop_request.is_file():
        return False
    print("Stop requested during startup; stopping metadata PostgreSQL before launching children.")
    command_pg_stop(context, argparse.Namespace(force=False))
    _unlink_if_exists(context.data_dir / "launcher.pid")
    _unlink_if_exists(stop_request)
    return True


def command_stop(context: LauncherContext, args: argparse.Namespace) -> int:
    force = bool(args.force)
    grace_seconds = float(args.grace_seconds)
    launcher_pid = _read_pid(context.data_dir / "launcher.pid")
    if launcher_pid is not None and launcher_pid != os.getpid() and _pid_alive(launcher_pid):
        print(
            "Stopping launcher supervisor. Active worker jobs run to completion; "
            "use --force only for emergency stop."
        )
        if _is_windows():
            stop_request = context.data_dir / "launcher.stop"
            stop_request.write_text("stop\n", encoding="ascii")
            _wait_for_pid_exit(
                launcher_pid,
                "launcher",
                grace_seconds=grace_seconds + 35.0,
                force=force,
            )
            _unlink_if_exists(stop_request)
        else:
            _terminate_pid(
                launcher_pid,
                "launcher",
                grace_seconds=grace_seconds,
                force=force,
            )
        _unlink_if_exists(context.data_dir / "launcher.pid")
        _unlink_child_pid_files(context)
        return 0

    print("Stopping API first so no new jobs are accepted.")
    _terminate_pid_file(context.data_dir / "api.pid", "api", grace_seconds=30.0, force=force)
    print(
        "Stopping worker gracefully. Active queries run to completion; "
        "use --force only for emergency stop."
    )
    _terminate_pid_file(
        context.data_dir / "worker.pid",
        "worker",
        grace_seconds=grace_seconds,
        force=force,
    )
    command_pg_stop(context, argparse.Namespace(force=force))
    _unlink_if_exists(context.data_dir / "launcher.pid")
    return 0


def command_status(context: LauncherContext, args: argparse.Namespace) -> int:
    del args
    for name in ("launcher", "api", "worker"):
        pid_file = context.data_dir / f"{name}.pid"
        pid = _read_pid(pid_file)
        state = "running" if pid is not None and _pid_alive(pid) else "stopped"
        print(f"{name}: {state}" + (f" pid={pid}" if pid is not None else ""))
    return command_pg_status(context, argparse.Namespace())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.launcher")
    parser.add_argument("--root", default=".", help="runtime root for config/, data/, logs/")
    parser.add_argument("--uv", default="uv", help="uv executable")
    parser.add_argument(
        "--pg-bin-dir",
        default=None,
        help="directory containing initdb/pg_ctl/postgres",
    )

    subparsers = parser.add_subparsers(dest="command")
    _set_command(subparsers.add_parser("doctor", help="check local prerequisites"), command_doctor)

    bootstrap = subparsers.add_parser("bootstrap", help="bootstrap secret commands")
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_command")
    bootstrap_init = bootstrap_sub.add_parser("init", help="generate bootstrap secret files")
    bootstrap_init.add_argument("--force", action="store_true", help="overwrite existing secrets")
    _set_command(bootstrap_init, command_bootstrap_init)

    _set_command(subparsers.add_parser("pg-up", help="initialize/start managed PG"), command_pg_up)
    pg_stop = subparsers.add_parser("pg-stop", help="stop managed PG")
    pg_stop.add_argument("--force", action="store_true", help="use immediate PG shutdown")
    _set_command(pg_stop, command_pg_stop)
    _set_command(subparsers.add_parser("pg-status", help="check managed PG"), command_pg_status)
    _set_command(
        subparsers.add_parser("alembic-up", help="run Alembic upgrade head"),
        command_alembic_up,
    )

    admin = subparsers.add_parser("admin", help="admin user commands")
    admin_sub = admin.add_subparsers(dest="admin_command")
    admin_create = admin_sub.add_parser("create", help="create or update an admin user")
    admin_create.add_argument("--username", required=True)
    admin_create.add_argument("--password", default=None)
    admin_create.add_argument("--role", default="admin")
    admin_create.add_argument("--project-name", default="Default")
    admin_create.add_argument("--update-password", action="store_true")
    _set_command(admin_create, command_admin_create)

    up = subparsers.add_parser("up", help="start PG, API, and worker")
    up.add_argument("--grace-seconds", type=float, default=600.0)
    _set_command(up, command_up)

    stop = subparsers.add_parser("stop", help="stop API, worker, and PG")
    stop.add_argument("--force", action="store_true")
    stop.add_argument("--grace-seconds", type=float, default=600.0)
    _set_command(stop, command_stop)
    _set_command(subparsers.add_parser("status", help="show launcher status"), command_status)
    return parser


def _set_command(parser: argparse.ArgumentParser, func: LauncherCommand) -> None:
    parser.set_defaults(func=func)


def _context_from_args(args: argparse.Namespace) -> LauncherContext:
    root = Path(str(args.root)).expanduser().resolve()
    settings = load_settings()
    _resolve_settings_paths(settings, root)
    pg_bin_dir = Path(str(args.pg_bin_dir)).expanduser().resolve() if args.pg_bin_dir else None
    return LauncherContext(
        root=root,
        repo_root=Path(__file__).resolve().parents[1],
        settings=settings,
        uv=str(args.uv),
        pg_bin_dir=pg_bin_dir,
    )


def _resolve_settings_paths(settings: Settings, root: Path) -> None:
    settings.bootstrap.master_key_file = _under_root(root, settings.bootstrap.master_key_file)
    settings.bootstrap.pg_app_password_file = _under_root(
        root, settings.bootstrap.pg_app_password_file
    )
    settings.bootstrap.pg_superuser_password_file = _under_root(
        root, settings.bootstrap.pg_superuser_password_file
    )
    settings.bootstrap.jwt_secret_file = _under_root(root, settings.bootstrap.jwt_secret_file)
    settings.bootstrap.license_file = _under_root(root, settings.bootstrap.license_file)
    settings.result_store.local_root = _under_root(root, settings.result_store.local_root)


def _under_root(root: Path, path: Path) -> Path:
    return path.expanduser().resolve() if path.is_absolute() else (root / path).resolve()


def _ensure_runtime_dirs(context: LauncherContext) -> None:
    context.config_dir.mkdir(parents=True, exist_ok=True)
    (context.config_dir / "secrets").mkdir(parents=True, exist_ok=True)
    context.data_dir.mkdir(parents=True, exist_ok=True)
    context.log_dir.mkdir(parents=True, exist_ok=True)
    context.settings.result_store.local_root.mkdir(parents=True, exist_ok=True)
    if not _is_windows():
        (context.config_dir / "secrets").chmod(SECRET_DIR_MODE)


def _bootstrap(context: LauncherContext) -> LocalFileBootstrapSecrets:
    return LocalFileBootstrapSecrets(
        master_key_file=context.settings.bootstrap.master_key_file,
        pg_app_password_file=context.settings.bootstrap.pg_app_password_file,
        pg_superuser_password_file=context.settings.bootstrap.pg_superuser_password_file,
        jwt_secret_file=context.settings.bootstrap.jwt_secret_file,
        license_file=context.settings.bootstrap.license_file,
    )


def _write_secret_file(path: Path, value: bytes, *, force: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        _ensure_secret_mode(path)
        return False
    if _is_windows():
        path.write_bytes(value)
        return True
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value)
    path.chmod(SECRET_FILE_MODE)
    return True


def _ensure_secret_mode(path: Path) -> None:
    if not _is_windows():
        path.chmod(SECRET_FILE_MODE)


def _sync_license_state(
    context: LauncherContext,
    bootstrap: LocalFileBootstrapSecrets,
) -> None:
    state = verify_license(context.settings.bootstrap.license_file)
    engine = _metadata_engine(
        context,
        user=context.settings.db.user,
        password=bootstrap.get_pg_app_password(),
        database=context.settings.db.database,
    )
    now = datetime.now(UTC)
    values = {
        "id": 1,
        "edition": state.edition,
        "customer": state.customer,
        "expires_at": state.expires_at,
        "features": state.features,
        "signature_verified_at": now if state.mode.value in {"valid", "in_grace"} else None,
        "grace_started_at": now if state.mode.value == "in_grace" else None,
        "mode": state.mode.value,
        "repair_reason": state.repair_reason,
        "updated_at": now,
    }
    try:
        with engine.begin() as conn:
            existing = (
                conn.execute(
                    select(
                        license_state.c.id,
                        license_state.c.mode,
                        license_state.c.expires_at,
                    ).where(license_state.c.id == 1)
                )
                .mappings()
                .one_or_none()
            )
            if (
                existing is not None
                and state.mode.value == "trial"
                and existing["mode"] == "trial"
                and existing["expires_at"] is not None
            ):
                values["expires_at"] = existing["expires_at"]
            if existing is None:
                conn.execute(insert(license_state).values(**values))
            else:
                conn.execute(update(license_state).where(license_state.c.id == 1).values(**values))
    finally:
        engine.dispose()
    if state.mode.value == "trial":
        _print_license_hint(context)
    elif state.mode.value == "repair":
        print(f"license state: repair required ({state.repair_reason})")
    else:
        print(f"license state: {state.mode.value}")


def _print_license_hint(context: LauncherContext) -> None:
    if context.settings.bootstrap.license_file.exists():
        return
    print(
        "license.lic not found; DataOpsStudio will start in 30-day trial mode. "
        f"Expected path: {context.settings.bootstrap.license_file}"
    )


def _initdb(context: LauncherContext) -> None:
    context.pgdata_dir.parent.mkdir(parents=True, exist_ok=True)
    initdb = _find_pg_binary(context, "initdb")
    print(f"Initializing PG data dir: {context.pgdata_dir}")
    _run(
        [
            str(initdb),
            "-D",
            str(context.pgdata_dir),
            "-U",
            DEFAULT_PG_SUPERUSER,
            "--auth-host=scram-sha-256",
            "--auth-local=trust",
            "--pwfile",
            str(context.settings.bootstrap.pg_superuser_password_file),
        ],
        cwd=context.repo_root,
        env=_child_env(context),
    )


def _pg_ctl_start(context: LauncherContext) -> None:
    pg_ctl = _find_pg_binary(context, "pg_ctl")
    pg_log = context.log_dir / "pg.log"
    postgres_options = f"-h {context.settings.db.host} -p {context.settings.db.port}"
    if not _is_windows():
        socket_dir = context.data_dir / "pg-socket"
        socket_dir.mkdir(parents=True, exist_ok=True)
        postgres_options += f" -k {socket_dir}"
    print(f"Starting PG: log={pg_log}")
    try:
        _run(
            [
                str(pg_ctl),
                "-D",
                str(context.pgdata_dir),
                "-l",
                str(pg_log),
                "-o",
                postgres_options,
                "start",
            ],
            cwd=context.repo_root,
            env=_child_env(context),
        )
    except LauncherError:
        _print_pg_log_tail(pg_log)
        raise


def _pg_ctl_stop(context: LauncherContext, *, force: bool) -> None:
    pg_ctl = _find_pg_binary(context, "pg_ctl")
    mode = "immediate" if force else "fast"
    result = subprocess.run(
        [str(pg_ctl), "-D", str(context.pgdata_dir), "stop", "-m", mode],
        cwd=context.repo_root,
        env=_child_env(context),
        check=False,
    )
    if result.returncode == 0:
        print("PG stopped.")
    else:
        print("PG stop returned non-zero; it may already be stopped.")


def _ensure_metadata_database(
    context: LauncherContext,
    bootstrap: LocalFileBootstrapSecrets,
) -> None:
    db_user = context.settings.db.user
    db_name = context.settings.db.database
    _validate_identifier(db_user, "DATAOPS_PG_USER")
    _validate_identifier(db_name, "DATAOPS_PG_DATABASE")
    engine = _metadata_engine(
        context,
        user=DEFAULT_PG_SUPERUSER,
        password=bootstrap.get_pg_superuser_password(),
        database="postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        app_password = bootstrap.get_pg_app_password()
        role_ident = _quote_ident(db_user)
        db_ident = _quote_ident(db_name)
        app_password_literal = _quote_literal(app_password)
        with engine.connect() as conn:
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
                {"name": db_user},
            ).scalar_one_or_none()
            if role_exists is None:
                conn.execute(
                    text(f"CREATE ROLE {role_ident} WITH LOGIN PASSWORD {app_password_literal}")
                )
                print(f"Created PG role: {db_user}")
            else:
                conn.execute(
                    text(f"ALTER ROLE {role_ident} WITH LOGIN PASSWORD {app_password_literal}")
                )

            db_exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).scalar_one_or_none()
            if db_exists is None:
                conn.execute(text(f"CREATE DATABASE {db_ident} OWNER {role_ident}"))
                print(f"Created PG database: {db_name}")
            conn.execute(text(f"GRANT ALL PRIVILEGES ON DATABASE {db_ident} TO {role_ident}"))
    finally:
        engine.dispose()


def _database_ready(
    context: LauncherContext,
    *,
    user: str,
    password: str,
    database: str,
    timeout_seconds: float,
) -> bool:
    engine = _metadata_engine(
        context,
        user=user,
        password=password,
        database=database,
        connect_timeout_seconds=timeout_seconds,
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        engine.dispose()


def _wait_for_database(
    context: LauncherContext,
    *,
    user: str,
    password: str,
    database: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _database_ready(
            context,
            user=user,
            password=password,
            database=database,
            timeout_seconds=1.0,
        ):
            return
        time.sleep(0.5)
    raise LauncherError("PG did not become ready before timeout")


def _metadata_engine(
    context: LauncherContext,
    *,
    user: str,
    password: str,
    database: str,
    isolation_level: str | None = None,
    connect_timeout_seconds: float | None = None,
) -> Engine:
    kwargs: dict[str, object] = {}
    if isolation_level is not None:
        kwargs["isolation_level"] = isolation_level
    if connect_timeout_seconds is not None:
        kwargs["connect_args"] = {"connect_timeout": int(max(connect_timeout_seconds, 1))}
    return create_engine(
        _metadata_database_url(context, user=user, password=password, database=database),
        pool_pre_ping=True,
        **kwargs,
    )


def _metadata_database_url(
    context: LauncherContext,
    *,
    user: str,
    password: str | None,
    database: str,
) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=context.settings.db.host,
        port=context.settings.db.port,
        database=database,
    )


def _child_env(context: LauncherContext) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("POSTGRES_DEV_PASSWORD", None)
    env.update(
        {
            "DATAOPS_BOOTSTRAP_MASTER_KEY_FILE": str(context.settings.bootstrap.master_key_file),
            "DATAOPS_BOOTSTRAP_PG_APP_PASSWORD_FILE": str(
                context.settings.bootstrap.pg_app_password_file
            ),
            "DATAOPS_BOOTSTRAP_PG_SUPERUSER_PASSWORD_FILE": str(
                context.settings.bootstrap.pg_superuser_password_file
            ),
            "DATAOPS_BOOTSTRAP_JWT_SECRET_FILE": str(context.settings.bootstrap.jwt_secret_file),
            "DATAOPS_BOOTSTRAP_LICENSE_FILE": str(context.settings.bootstrap.license_file),
            "DATAOPS_RESULT_LOCAL_ROOT": str(context.settings.result_store.local_root),
            "DATAOPS_PG_HOST": context.settings.db.host,
            "DATAOPS_PG_PORT": str(context.settings.db.port),
            "DATAOPS_PG_USER": context.settings.db.user,
            "DATAOPS_PG_DATABASE": context.settings.db.database,
            "DATAOPS_API_HOST": context.settings.api.host,
            "DATAOPS_API_PORT": str(context.settings.api.port),
            "DATAOPS_WORKER_WORKER_ID": context.settings.worker.worker_id,
            "DATAOPS_WORKER_POLL_INTERVAL_SECONDS": str(
                context.settings.worker.poll_interval_seconds
            ),
            "DATAOPS_WORKER_RESULT_GC_INTERVAL_SECONDS": str(
                context.settings.worker.result_gc_interval_seconds
            ),
        }
    )
    return env


def _start_child(
    context: LauncherContext,
    name: str,
    command: list[str],
) -> subprocess.Popen[bytes]:
    log_path = context.log_dir / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if _is_windows():
        creationflags = cast(Any, subprocess).CREATE_NEW_PROCESS_GROUP
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=context.repo_root,
            env=_child_env(context),
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        handle.close()
    _write_pid(context.data_dir / f"{name}.pid", process.pid)
    print(f"started {name}: pid={process.pid}, log={log_path}")
    return process


def _stop_children(
    children: dict[str, subprocess.Popen[bytes]],
    *,
    force: bool,
    grace_seconds: float,
) -> None:
    api = children.get("api")
    if api is not None:
        _terminate_process(api, "api", grace_seconds=30.0, force=force)
    worker = children.get("worker")
    if worker is not None:
        _terminate_process(worker, "worker", grace_seconds=grace_seconds, force=force)


def _unlink_child_pid_files(context: LauncherContext) -> None:
    _unlink_if_exists(context.data_dir / "api.pid")
    _unlink_if_exists(context.data_dir / "worker.pid")


def _terminate_process(
    process: subprocess.Popen[bytes],
    name: str,
    *,
    grace_seconds: float,
    force: bool,
) -> None:
    if process.poll() is not None:
        return
    if _is_windows():
        process.send_signal(cast(Any, signal).CTRL_BREAK_EVENT)
    else:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        print(f"{name} stopped.")
    except subprocess.TimeoutExpired:
        if force:
            _kill_pid(process.pid)
            process.wait(timeout=10)
            print(f"{name} force-stopped.")
        else:
            raise LauncherError(
                f"{name} is still draining an active job after {grace_seconds:.0f}s. "
                "Wait and retry, or use stop --force for emergency stop."
            ) from None


def _terminate_pid_file(
    pid_file: Path,
    name: str,
    *,
    grace_seconds: float,
    force: bool,
) -> None:
    pid = _read_pid(pid_file)
    if pid is None:
        print(f"{name}: no pid file")
        return
    if not _pid_alive(pid):
        print(f"{name}: stale pid file removed")
        _unlink_if_exists(pid_file)
        return
    _terminate_pid(pid, name, grace_seconds=grace_seconds, force=force)
    _unlink_if_exists(pid_file)


def _terminate_pid(
    pid: int,
    name: str,
    *,
    grace_seconds: float,
    force: bool,
) -> None:
    os.kill(pid, signal.SIGTERM)
    _wait_for_pid_exit(
        pid,
        name,
        grace_seconds=grace_seconds,
        force=force,
    )


def _wait_for_pid_exit(
    pid: int,
    name: str,
    *,
    grace_seconds: float,
    force: bool,
) -> None:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            print(f"{name} stopped.")
            return
        time.sleep(0.5)
    if force:
        _kill_pid(pid)
        print(f"{name} force-stopped.")
        return
    raise LauncherError(
        f"{name} is still draining after {grace_seconds:.0f}s. "
        "Large queries can make graceful stop take a while; use --force only in emergencies."
    )


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if _is_windows():
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    windows_ctypes = cast(Any, ctypes)
    get_last_error = cast(Callable[[], int], windows_ctypes.get_last_error)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _kill_pid(pid: int) -> None:
    if _is_windows():
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 and _windows_pid_alive(pid):
            raise LauncherError(f"failed to force-stop process tree pid={pid}")
        return
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    os.kill(pid, sigkill)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _find_pg_binary(
    context: LauncherContext,
    name: str,
    *,
    required: bool = True,
) -> Path | None:
    if context.pg_bin_dir is not None:
        candidate = context.pg_bin_dir / name
        if _is_windows():
            exe_candidate = context.pg_bin_dir / f"{name}.exe"
            candidate = exe_candidate if exe_candidate.exists() else candidate
        if candidate.exists():
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    if required:
        raise LauncherError(
            f"PostgreSQL binary not found: {name}. Install bundled PG or pass --pg-bin-dir."
        )
    return None


def _which(command: str) -> Path | None:
    found = shutil.which(command)
    if found:
        return Path(found)
    if Path(command).is_absolute() and Path(command).exists():
        return Path(command)
    for directory in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        candidate = directory / command
        if candidate.exists():
            return candidate
    return None


def _uv_missing_detail(command: str) -> str:
    return f"command={command} not found on PATH; checked PATH plus ~/.local/bin and ~/.cargo/bin"


def _tcp_port_available(host: str, port: int) -> bool:
    try:
        with socket.create_connection((_connect_host(host), port), timeout=0.5):
            return False
    except OSError:
        return True


def _connect_host(host: str) -> str:
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host


def _print_pg_log_tail(path: Path, *, line_count: int = 5) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        print(f"PG failed to start; log file does not exist yet: {path}", file=sys.stderr)
        return
    tail = lines[-line_count:]
    print(f"PG failed to start; last {len(tail)} lines from {path}:", file=sys.stderr)
    for line in tail:
        print(line, file=sys.stderr)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        display = " ".join(command[:3])
        raise LauncherError(f"command failed: {display} ... (exit={result.returncode})")


def _validate_identifier(value: str, name: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise LauncherError(f"{name} must be a simple PostgreSQL identifier")


def _quote_ident(value: str) -> str:
    _validate_identifier(value, "identifier")
    return f'"{value}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _doctor_check(name: str, passed: bool, detail: str) -> bool:
    marker = "OK" if passed else "MISSING"
    print(f"{marker:7} {name}: {detail}")
    return passed


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


if __name__ == "__main__":
    raise SystemExit(main())
