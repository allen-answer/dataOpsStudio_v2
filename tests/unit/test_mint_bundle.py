from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from tools.package import build_mint_bundle as build
from tools.package.build_identity import BuildIdentity


def _identity() -> BuildIdentity:
    return BuildIdentity(
        version="2.0.1",
        commit="a" * 40,
        image_version="2.0.1-mint21.3-amd64-offline",
    )


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires Unix bash")
def test_common_script_exports_packaged_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(build, "BUNDLE_ROOT", bundle)
    build.write_scripts(_identity())
    common = bundle / "_common.sh"
    assert build.BUILD_IDENTITY_MARKER not in common.read_text(encoding="utf-8")
    assert common.stat().st_size <= (build.SCRIPTS_ROOT / "_common.sh").stat().st_size
    env = os.environ.copy()
    env["DATAOPS_BUILD_VERSION"] = "stale"
    env["DATAOPS_BUILD_COMMIT"] = "b" * 40
    env["DATAOPS_IMAGE_VERSION"] = "stale"
    env["HOME"] = str(tmp_path / "home")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s|%s|%s" "$DATAOPS_BUILD_VERSION" '
            '"$DATAOPS_BUILD_COMMIT" "$DATAOPS_IMAGE_VERSION"',
            "build-identity-test",
            str(common),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout == f"2.0.1|{'a' * 40}|2.0.1-mint21.3-amd64-offline"


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
