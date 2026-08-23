from __future__ import annotations

import gzip
from io import BytesIO
from pathlib import Path
from typing import Any

from tools.package import build_mint_bundle as build


def _gzip(payload: bytes, *, compresslevel: int, mtime: int) -> bytes:
    output = BytesIO()
    with gzip.GzipFile(
        filename="fixture.tar",
        mode="wb",
        compresslevel=compresslevel,
        fileobj=output,
        mtime=mtime,
    ) as archive:
        archive.write(payload)
    return output.getvalue()


def _write_ar(path: Path, members: list[tuple[str, bytes]]) -> None:
    with path.open("wb") as archive:
        archive.write(b"!<arch>\n")
        for name, payload in members:
            header = b"".join(
                (
                    f"{name}/".encode().ljust(16),
                    b"1234567890".ljust(12),
                    b"1000".ljust(6),
                    b"1000".ljust(6),
                    b"100664".ljust(8),
                    str(len(payload)).encode().ljust(10),
                    b"`\n",
                )
            )
            assert len(header) == 60
            archive.write(header)
            archive.write(payload)
            if len(payload) % 2:
                archive.write(b"\n")


def _read_ar(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    assert data.startswith(b"!<arch>\n")
    offset = 8
    members: dict[str, bytes] = {}
    while offset < len(data):
        header = data[offset : offset + 60]
        assert len(header) == 60 and header[58:60] == b"`\n"
        offset += 60
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58].decode("ascii").strip())
        members[name] = data[offset : offset + size]
        offset += size + size % 2
    return members


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


def test_repack_deb_is_deterministic_and_preserves_member_payloads(tmp_path: Path) -> None:
    control_tar = (b"control-metadata\n" * 2_000) + bytes(range(256))
    data_tar = (b"runtime-payload\n" * 20_000) + bytes(range(256))
    source = tmp_path / "source.deb"
    _write_ar(
        source,
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", _gzip(control_tar, compresslevel=1, mtime=123)),
            ("data.tar.gz", _gzip(data_tar, compresslevel=1, mtime=123)),
        ],
    )

    first = tmp_path / "first.deb"
    second = tmp_path / "second.deb"
    build.repack_deb(source, first)
    build.repack_deb(source, second)

    members = _read_ar(first)
    assert members["debian-binary"] == b"2.0\n"
    assert gzip.decompress(members["control.tar.gz"]) == control_tar
    assert gzip.decompress(members["data.tar.gz"]) == data_tar
    assert first.read_bytes() == second.read_bytes()
    assert first.stat().st_size < source.stat().st_size
