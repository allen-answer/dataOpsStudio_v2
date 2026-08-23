"""Build the Linux Mint 21.3 x86_64 offline Tauri ``.deb`` package.

This builder must run on the oldest supported target (Mint 21.3 / glibc 2.35).
It downloads only checksum-pinned upstream release artifacts, builds a slim and
relocatable PostgreSQL 16 installation from the official source tarball, builds
the Vue frontend with npm, assembles the offline runtime as Tauri resources and
then invokes ``cargo tauri build --bundles deb``.

No credential or instance state is ever placed in the bundle. Large outputs are
kept below ``dist-mint/`` and ``desktop/bundle/``, both git-ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "desktop"
BUNDLE_ROOT = DESKTOP_ROOT / "bundle"
SCRIPTS_ROOT = REPO_ROOT / "bundle-scripts"

PYTHON_VERSION = "3.12.13"
PYTHON_RELEASE = "20260610"
PYTHON_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_RELEASE}/cpython-{PYTHON_VERSION}%2B{PYTHON_RELEASE}"
    "-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
)
PYTHON_SHA256 = "6682afef6b510037a0ad84e61150e5121af2e2785c9ca27b047e029270a840fe"

UV_VERSION = "0.11.23"
UV_URL = (
    f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
    "uv-x86_64-unknown-linux-gnu.tar.gz"
)
UV_SHA256 = "e12c4cda2fe8c305510a78380a88f2c32a27e90cdcd123cefd2873388f0ebb5f"

POSTGRES_VERSION = "16.15"
POSTGRES_URL = (
    f"https://ftp.postgresql.org/pub/source/v{POSTGRES_VERSION}/"
    f"postgresql-{POSTGRES_VERSION}.tar.bz2"
)
POSTGRES_SHA256 = "c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed"

SOURCE_ITEMS = (
    "app",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)
SCRIPT_ITEMS = ("_common.sh", "install.sh", "start.sh", "stop.sh", "README.md")
PG_REQUIRED_BINS = {"initdb", "pg_ctl", "postgres"}
# Exact last verified Mint package size (172.27 MiB rounded for display).
MAX_DEB_BYTES = 180_636_606


def log(message: str) -> None:
    print(f"[build-mint] {message}", flush=True)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    log("run: " + " ".join(command))
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, sha256: str, destination: Path) -> Path:
    if destination.exists() and sha256_file(destination) == sha256:
        log(f"download: reuse verified {destination.name}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    log(f"download: {url}")
    with urllib.request.urlopen(url) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256_file(temporary)
    if actual != sha256:
        temporary.unlink(missing_ok=True)
        raise SystemExit(
            f"sha256 mismatch for {destination.name}: expected={sha256} actual={actual}"
        )
    temporary.replace(destination)
    log(f"download: verified sha256={actual}")
    return destination


def copytree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", maxsplit=1)[1].split("[", maxsplit=1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if match is None:
        raise SystemExit("cannot read [project].version from pyproject.toml")
    return match.group(1)


def prepare_python(cache: Path) -> Path:
    destination = cache / "python"
    executable = destination / "bin/python3"
    if executable.exists():
        log(f"python: reuse {destination}")
        slim_python(destination)
        return destination
    archive = download(PYTHON_URL, PYTHON_SHA256, cache / "downloads/python.tar.gz")
    temporary = cache / "python.extract"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    log(f"python: extract {PYTHON_VERSION}")
    run(["tar", "-xzf", str(archive), "-C", str(temporary)])
    extracted = temporary / "python"
    if not (extracted / "bin/python3").exists():
        raise SystemExit("python archive did not contain python/bin/python3")
    extracted.replace(destination)
    shutil.rmtree(temporary, ignore_errors=True)
    slim_python(destination)
    return destination


def slim_python(destination: Path) -> None:
    """Drop embedding/development/Tk assets not used by the headless backend.

    The selected python-build-standalone executable is self-contained (it does
    not link libpython), while third-party manylinux extensions do not link it
    either. Keep ensurepip because install.sh creates a venv, but remove the
    separate embedding library, C headers, Tk stack and terminal database.
    """

    remove = (
        destination / "include",
        destination / "share",
        destination / "lib/libpython3.12.so",
        destination / "lib/libpython3.12.so.1.0",
        destination / "lib/libpython3.so",
        destination / "lib/libtcl9.0.so",
        destination / "lib/libtcl9tk9.0.so",
        destination / "lib/pkgconfig",
        destination / "lib/tcl9",
        destination / "lib/tcl9.0",
        destination / "lib/thread3.0.4",
        destination / "lib/tk9.0",
        destination / "lib/itcl4.3.5",
        destination / "lib/python3.12/idlelib",
        destination / "lib/python3.12/tkinter",
        destination / "lib/python3.12/turtledemo",
        destination / "lib/python3.12/lib-dynload/_tkinter.cpython-312-x86_64-linux-gnu.so",
    )
    for path in remove:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    run(
        [
            str(destination / "bin/python3"),
            "-c",
            "import ctypes, multiprocessing, ssl, venv; print('slim Python runtime OK')",
        ]
    )


def prepare_uv(cache: Path) -> Path:
    destination = cache / "uv"
    executable = destination / "uv"
    if executable.exists():
        log(f"uv: reuse {executable}")
        return executable
    archive = download(UV_URL, UV_SHA256, cache / "downloads/uv.tar.gz")
    temporary = cache / "uv.extract"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    run(["tar", "-xzf", str(archive), "-C", str(temporary)])
    candidates = list(temporary.rglob("uv"))
    if not candidates:
        raise SystemExit("uv archive did not contain uv")
    destination.mkdir(parents=True)
    shutil.copy2(candidates[0], executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.rmtree(temporary, ignore_errors=True)
    return executable


def export_requirements(cache: Path, uv: Path) -> Path:
    destination = cache / "requirements-frozen.txt"
    log("requirements: export locked default runtime dependencies (no optional previews)")
    run(
        [
            str(uv),
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "-o",
            str(destination),
        ],
        cwd=REPO_ROOT,
    )
    return destination


def prepare_wheels(cache: Path, python: Path, requirements: Path) -> Path:
    destination = cache / "wheels"
    marker = destination / ".complete"
    if marker.exists() and any(destination.glob("*.whl")):
        log(f"wheels: reuse {destination} ({len(list(destination.glob('*.whl')))} files)")
        return destination
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    run(
        [
            str(python / "bin/python3"),
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            str(requirements),
            "-d",
            str(destination),
        ]
    )
    marker.write_text("verified\n", encoding="ascii")
    return destination


def prepare_postgres(cache: Path, jobs: int) -> Path:
    destination = cache / f"postgresql-{POSTGRES_VERSION}"
    if all((destination / "bin" / name).exists() for name in PG_REQUIRED_BINS):
        log(f"postgres: reuse {destination}")
        return destination

    archive = download(
        POSTGRES_URL,
        POSTGRES_SHA256,
        cache / f"downloads/postgresql-{POSTGRES_VERSION}.tar.bz2",
    )
    source_parent = cache / "postgres-source"
    build = cache / "postgres-build"
    install = cache / "postgres-install"
    for path in (source_parent, build, install, destination):
        shutil.rmtree(path, ignore_errors=True)
    source_parent.mkdir(parents=True)
    build.mkdir(parents=True)
    install.mkdir(parents=True)
    log(f"postgres: extract official source {POSTGRES_VERSION}")
    run(["tar", "-xjf", str(archive), "-C", str(source_parent)])
    source = source_parent / f"postgresql-{POSTGRES_VERSION}"
    prefix = install / "pgsql"
    run(
        [
            str(source / "configure"),
            f"--prefix={prefix}",
            "--disable-rpath",
            "--without-icu",
            "--without-readline",
            "--without-zlib",
        ],
        cwd=build,
    )
    run(["make", f"-j{jobs}"], cwd=build)
    run(["make", "install"], cwd=build)

    bin_dir = prefix / "bin"
    for binary in bin_dir.iterdir():
        if binary.name not in PG_REQUIRED_BINS:
            binary.unlink()
    shutil.rmtree(prefix / "include", ignore_errors=True)
    shutil.rmtree(prefix / "lib/pkgconfig", ignore_errors=True)
    shutil.rmtree(prefix / "lib/pgxs", ignore_errors=True)
    for static_library in (prefix / "lib").glob("*.a"):
        static_library.unlink()
    shutil.rmtree(prefix / "share/doc", ignore_errors=True)
    shutil.rmtree(prefix / "share/man", ignore_errors=True)
    shutil.copy2(source / "COPYRIGHT", prefix / "server_license.txt")

    strip = shutil.which("strip")
    if strip:
        strip_targets = [*bin_dir.iterdir(), *(prefix / "lib").glob("*.so*")]
        for target in strip_targets:
            if target.is_file() and not target.is_symlink():
                subprocess.run([strip, "--strip-unneeded", str(target)], check=False)

    prefix.replace(destination)
    shutil.rmtree(source_parent, ignore_errors=True)
    shutil.rmtree(build, ignore_errors=True)
    shutil.rmtree(install, ignore_errors=True)
    return destination


def build_frontend(skip: bool) -> Path:
    frontend = REPO_ROOT / "frontend"
    destination = frontend / "dist"
    if skip:
        if not (destination / "index.html").exists():
            raise SystemExit("--skip-frontend-build requires frontend/dist/index.html")
        log("frontend: reuse existing dist")
        return destination
    run(["npm", "ci"], cwd=frontend)
    run(["npm", "run", "build"], cwd=frontend)
    if not (destination / "index.html").exists():
        raise SystemExit("frontend build did not produce frontend/dist/index.html")
    return destination


def assemble(
    *,
    python: Path,
    uv: Path,
    wheels: Path,
    postgres: Path,
    requirements: Path,
    frontend: Path,
) -> None:
    shutil.rmtree(BUNDLE_ROOT, ignore_errors=True)
    BUNDLE_ROOT.mkdir(parents=True)
    for item in SOURCE_ITEMS:
        source = REPO_ROOT / item
        destination = BUNDLE_ROOT / item
        copytree(source, destination) if source.is_dir() else shutil.copy2(source, destination)
    copytree(frontend, BUNDLE_ROOT / "frontend/dist")
    copytree(python, BUNDLE_ROOT / "runtime/python")
    (BUNDLE_ROOT / "runtime/uv").mkdir(parents=True)
    shutil.copy2(uv, BUNDLE_ROOT / "runtime/uv/uv")
    (BUNDLE_ROOT / "runtime/wheels").mkdir(parents=True)
    for wheel in wheels.glob("*.whl"):
        shutil.copy2(wheel, BUNDLE_ROOT / "runtime/wheels" / wheel.name)
    shutil.copy2(requirements, BUNDLE_ROOT / "runtime/requirements-frozen.txt")
    copytree(postgres, BUNDLE_ROOT / "pgsql")
    for name in SCRIPT_ITEMS:
        shutil.copy2(SCRIPTS_ROOT / name, BUNDLE_ROOT / name)
    for name in ("_common.sh", "install.sh", "start.sh", "stop.sh"):
        script = BUNDLE_ROOT / name
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def component_sizes() -> dict[str, int]:
    components = {
        "python": BUNDLE_ROOT / "runtime/python",
        "postgresql": BUNDLE_ROOT / "pgsql",
        "wheels": BUNDLE_ROOT / "runtime/wheels",
        "frontend": BUNDLE_ROOT / "frontend/dist",
        "application": BUNDLE_ROOT / "app",
    }
    return {name: tree_size(path) for name, path in components.items()}


def find_deb() -> Path:
    bundle_dir = DESKTOP_ROOT / "src-tauri/target/release/bundle/deb"
    candidates = sorted(bundle_dir.glob("*.deb"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"Tauri did not produce a .deb below {bundle_dir}")
    return candidates[-1]


def write_manifest(output_dir: Path, deb: Path, sizes: dict[str, int]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_deb = output_dir / f"dataops-studio-{project_version()}-mint21.3-amd64-offline.deb"
    shutil.copy2(deb, output_deb)
    total = output_deb.stat().st_size
    lines = [
        "DataOpsStudio Linux Mint 21.3 x86_64 offline GUI bundle",
        f"bundle_version: {project_version()}",
        "build_os: Linux Mint 21.3 (jammy, glibc 2.35, x86_64)",
        f"python: python-build-standalone {PYTHON_VERSION}+{PYTHON_RELEASE}",
        f"postgresql: {POSTGRES_VERSION} (official source, relocatable, slim)",
        f"uv: {UV_VERSION}",
        "wheels: locked default runtime dependencies (offline, --require-hashes)",
        "gui: Tauri v2 / webkit2gtk-4.1 / deb",
        f"deb: {output_deb.name}",
        f"deb_sha256: {sha256_file(output_deb)}",
        f"deb_size_bytes: {total}",
        f"deb_size_mib: {total / 1024 / 1024:.2f}",
        "component_uncompressed_bytes:",
        *[f"  {name}: {size} ({size / 1024 / 1024:.2f} MiB)" for name, size in sizes.items()],
    ]
    manifest = output_dir / f"{output_deb.name}.MANIFEST.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        log(line)
    if total > MAX_DEB_BYTES:
        raise SystemExit(f"size gate failed: {total / 1024 / 1024:.2f} MiB exceeds 172.27 MiB")
    return manifest


def validate_host() -> None:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise SystemExit("build_mint_bundle.py must run on Linux x86_64")
    libc_name, libc_version = os.confstr("CS_GNU_LIBC_VERSION").split()
    if libc_name != "glibc" or tuple(map(int, libc_version.split("."))) > (2, 35):
        raise SystemExit(
            f"build host is too new: {libc_name} {libc_version}; require <= glibc 2.35"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / "dist-mint/cache"))
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "dist-mint"))
    parser.add_argument("--skip-frontend-build", action="store_true")
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    args = parser.parse_args(argv)

    validate_host()
    cache = Path(args.cache_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)

    python = prepare_python(cache)
    uv = prepare_uv(cache)
    requirements = export_requirements(cache, uv)
    wheels = prepare_wheels(cache, python, requirements)
    postgres = prepare_postgres(cache, args.jobs)
    frontend = build_frontend(args.skip_frontend_build)
    assemble(
        python=python,
        uv=uv,
        wheels=wheels,
        postgres=postgres,
        requirements=requirements,
        frontend=frontend,
    )
    sizes = component_sizes()
    run(["cargo", "tauri", "build", "--bundles", "deb"], cwd=DESKTOP_ROOT)
    deb = find_deb()
    write_manifest(output_dir, deb, sizes)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
