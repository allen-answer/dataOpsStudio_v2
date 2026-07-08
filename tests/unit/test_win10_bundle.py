from __future__ import annotations

import zipfile
from pathlib import Path

from tools.package import build_win10_bundle as build


def test_start_script_quotes_launcher_paths(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    start = (tmp_path / "start.bat").read_text(encoding="ascii")

    assert 'set "LAUNCH=' not in start
    assert "call :launcher bootstrap init || goto :fail" in start
    assert (
        '"%VENVPY%" -m app.launcher --root "%DATAOPS_HOME%" --pg-bin-dir "%PGBIN%"'
        ' --uv "%UVEXE%" %*'
    ) in start


def test_scripts_are_windows_ascii_crlf(tmp_path: Path) -> None:
    build.write_scripts(tmp_path)

    for name in ("start.bat", "stop.bat"):
        data = (tmp_path / name).read_bytes()
        data.decode("ascii")
        assert b"\r\n" in data


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
