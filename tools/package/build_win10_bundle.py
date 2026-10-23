"""Build a Win10 x64 deployment bundle for DataOpsStudio 2.0 (offline or online).

Two modes share one set of PowerShell install/start scripts (``install.ps1`` /
``start.ps1`` / ``stop.ps1`` + a one-line ``start.cmd`` shim), so there is no
drift between an air-gapped and a network install:

* ``--mode offline`` (default) — ``dataops-studio-<ver>-win10-x64-offline.zip``
  is self-contained: an air-gapped host unzips it and runs ``start.cmd``, no
  Python / network / separate PostgreSQL required. The bundle carries a
  relocatable standalone CPython 3.12, ``uv.exe``, every runtime wheel (cp312
  win_amd64, offline ``pip install --no-index`` installable), PostgreSQL 16
  server binaries (slim bin/lib/share), the built Vue SPA and the app source.

* ``--profile server2019`` — ``dataops-studio-<ver>-server2019-x64-offline.zip``
  is the offline bundle trimmed for a clean Windows Server 2019 host: no Tauri
  shell (no WebView2 there; use a browser), the VC++ runtime PostgreSQL imports
  shipped app-locally in ``pgsql/bin`` (Server 2019 has no VC++ 2015-2022
  redistributable, and EDB's binaries zip does not carry it), and the PG
  client/dev tooling the launcher never runs dropped.

* ``--mode online`` — ``dataops-studio-<ver>-win10-x64-online.zip`` is light: it
  carries only the app source, ``frontend/dist``, the scripts, the pinned
  ``requirements-frozen.txt`` (with sha256), and ``download-manifest.json``
  pinning the Python runtime / ``uv`` / PostgreSQL 16 URLs + sha256. The target
  downloads + verifies those on first run and installs deps from PyPI
  (hash-checked). No wheels / python / pg are shipped in the zip.

Zero secrets are ever written into either bundle (R8). The bootstrap master key,
PG passwords and admin password are generated/entered on the target host at
first run, never in git and never in this package.

DM (达梦) note: the dmPython *driver wheel* is bundled/downloaded (a locked main
dependency with a cp312 win_amd64 wheel), but the proprietary DM *client*
(DM_HOME crypto libraries) is NOT — real DM connectivity needs a separate DM
client install, see docs/deployment/win10-offline-bundle.md.

Usage (build host needs network + uv + Node/npm)::

    uv run python tools/package/build_win10_bundle.py                  # offline
    uv run python tools/package/build_win10_bundle.py --gui            # offline GUI
    uv run python tools/package/build_win10_bundle.py --mode online    # online
    uv run python tools/package/build_win10_bundle.py --profile server2019  # Server 2019 lite

Large artifacts land in ``dist-bundle/`` (git-ignored); the bundle itself never
enters git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.package.build_identity import (
        BuildIdentity,
        inject_build_identity,
        render_powershell_build_identity,
        resolve_build_identity,
    )
elif __package__:
    from .build_identity import (
        BuildIdentity,
        inject_build_identity,
        render_powershell_build_identity,
        resolve_build_identity,
    )
else:
    from build_identity import (
        BuildIdentity,
        inject_build_identity,
        render_powershell_build_identity,
        resolve_build_identity,
    )

if __package__:
    from .update_compatibility import (
        build_update_compatibility,
    )
    from .update_compatibility import (
        write_update_metadata as write_shared_update_metadata,
    )
else:
    from update_compatibility import (  # type: ignore[no-redef]
        build_update_compatibility,
    )
    from update_compatibility import (
        write_update_metadata as write_shared_update_metadata,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SCRIPTS_ROOT = REPO_ROOT / "bundle-scripts"
DESKTOP_ROOT = REPO_ROOT / "desktop"

# Pinned downstream artifacts. Recorded (not silently trusted): the script
# prints the sha256 it actually saw so a reviewer can compare across rebuilds.
PG_VERSION = "16.14-1"
PG_URL = f"https://get.enterprisedb.com/postgresql/postgresql-{PG_VERSION}-windows-x64-binaries.zip"
PG_SHA256 = "98af1417ba6a8dc30543e560e5407833a3b9e7cc7ed20e73b2006f3aa2f04663"
# PG components to keep (server-only slim set). pgAdmin (~670MB), StackBuilder,
# doc and include are dropped — the launcher only needs initdb/pg_ctl/postgres.
PG_KEEP_DIRS = ("bin", "lib", "share")
# server2019 profile: the only PG executables kept. The launcher runs initdb /
# pg_ctl / postgres; the rest are for an operator's backup/restore and recovery.
SERVER2019_PG_EXES = frozenset(
    {
        "initdb.exe",
        "pg_ctl.exe",
        "postgres.exe",
        "psql.exe",
        "pg_dump.exe",
        "pg_dumpall.exe",
        "pg_restore.exe",
        "pg_isready.exe",
        "pg_controldata.exe",
        "pg_resetwal.exe",
    }
)
# DLLs in pgsql/bin used only by dropped tools: StackBuilder (wxWidgets, curl),
# ecpg and the regression-test plugin.
SERVER2019_PG_DROP_DLL_PREFIXES = ("wx", "libcurl", "libecpg", "libpgtypes", "testplug")
# VC++ runtime DLLs imported by postgres.exe / initdb / pg_ctl / libpq / ICU.
VC_RUNTIME_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
# Exact last verified Win10 GUI package size (189 MiB rounded for display).
MAX_GUI_ZIP_BYTES = 198_142_107

# ── online-mode download pins ────────────────────────────────────────────────
# The online bundle ships no runtime; install.ps1 downloads these on the target
# and verifies each sha256. Pins are the *same* python-build-standalone build uv
# manages on the build host (see download-metadata) and the *same* uv release, so
# offline and online produce an identical runtime. Bump in lockstep with uv's
# managed CPython 3.12 patch when refreshing (see PR notes / playbook).
PYTHON_STANDALONE_TAG = "20260610"
PYTHON_FULL_VERSION = "3.12.13"
PYTHON_STANDALONE_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_STANDALONE_TAG}/cpython-{PYTHON_FULL_VERSION}%2B{PYTHON_STANDALONE_TAG}"
    "-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
)
PYTHON_STANDALONE_SHA256 = "99dce0b23bf3c3b28d350cdd7bfe3cd3be51cc4f285faae7c0df110d106d1a8d"
UV_VERSION = "0.11.23"
UV_URL = (
    f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"
)
UV_SHA256 = "02ad29f07e674d68726ba3bb1ff25b335d83515756e2b1a194bb56c3cc30e07c"

# Application source copied into the bundle (everything `python -m app.*` and
# `alembic upgrade head` need at runtime, cwd = bundle root).
SOURCE_ITEMS = (
    "app",
    "updater",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)


def log(message: str) -> None:
    print(f"[build-win10] {message}", flush=True)


def run(command: list[str], *, cwd: Path | None = None) -> None:
    log("run: " + " ".join(command))
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


# ── cache preparation ─────────────────────────────────────────────────────────


def export_requirements(cache: Path, uv: str) -> Path:
    target = cache / "requirements-frozen.txt"
    log("exporting locked requirements (main + oracle + db2 extras, no dev)")
    run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--extra",
            "oracle",
            "--extra",
            "db2",
            "--format",
            "requirements-txt",
            "-o",
            str(target),
        ],
        cwd=REPO_ROOT,
    )
    return target


def prepare_python(cache: Path) -> Path:
    """Return the checksum-pinned relocatable CPython runtime."""

    dest = cache / "python"
    marker = cache / "python.source.sha256"
    if (
        (dest / "python.exe").exists()
        and marker.exists()
        and marker.read_text(encoding="ascii").strip() == PYTHON_STANDALONE_SHA256
    ):
        log(f"python: reuse cached {dest}")
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    archive = _download_verified(
        PYTHON_STANDALONE_URL,
        PYTHON_STANDALONE_SHA256,
        cache / "downloads" / "python.tar.gz",
    )
    extracted = cache / "python.extract"
    shutil.rmtree(extracted, ignore_errors=True)
    extracted.mkdir(parents=True)
    run(["tar.exe", "-xzf", str(archive), "-C", str(extracted)])
    runtime = extracted / "python"
    if not (runtime / "python.exe").exists():
        raise SystemExit("python archive did not contain python/python.exe")
    runtime.replace(dest)
    shutil.rmtree(extracted, ignore_errors=True)
    marker.write_text(PYTHON_STANDALONE_SHA256 + "\n", encoding="ascii")
    return dest


def prepare_uv(cache: Path) -> Path:
    dest = cache / "uv"
    target = dest / "uv.exe"
    marker = cache / "uv.source.sha256"
    if (
        target.exists()
        and marker.exists()
        and marker.read_text(encoding="ascii").strip() == UV_SHA256
    ):
        log(f"uv: reuse cached {target}")
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    archive = _download_verified(UV_URL, UV_SHA256, cache / "downloads" / "uv.zip")
    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as source:
        source.extractall(dest)
    candidates = list(dest.rglob("uv.exe"))
    if not candidates:
        raise SystemExit("uv archive did not contain uv.exe")
    if candidates[0] != target:
        shutil.copy2(candidates[0], target)
    marker.write_text(UV_SHA256 + "\n", encoding="ascii")
    return dest


def download_wheels(cache: Path, python_dir: Path, requirements: Path) -> Path:
    wheels = cache / "wheels"
    requirements_hash = sha256_file(requirements)
    marker = cache / "wheels.requirements.sha256"
    if (
        wheels.exists()
        and any(wheels.glob("*.whl"))
        and marker.exists()
        and marker.read_text(encoding="ascii").strip() == requirements_hash
    ):
        log(f"wheels: reuse cached {wheels} ({len(list(wheels.glob('*.whl')))} files)")
        return wheels
    shutil.rmtree(wheels, ignore_errors=True)
    wheels.mkdir(parents=True)
    log("wheels: pip download (cp312 win_amd64, --only-binary=:all:)")
    run(
        [
            str(python_dir / "python.exe"),
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            str(requirements),
            "-d",
            str(wheels),
        ]
    )
    marker.write_text(requirements_hash + "\n", encoding="ascii")
    return wheels


def download_pg(cache: Path) -> Path:
    return _download_verified(PG_URL, PG_SHA256, cache / "downloads" / "pg16.zip")


def _download_verified(url: str, sha256: str, dest: Path) -> Path:
    import urllib.request

    if dest.exists() and sha256_file(dest) == sha256:
        log(f"download: reuse verified {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.unlink(missing_ok=True)
    log(f"download: {url}")
    with urllib.request.urlopen(url) as response:
        with tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    actual = sha256_file(tmp)
    if actual != sha256:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"sha256 mismatch for {dest.name}: expected={sha256} actual={actual}")
    tmp.replace(dest)
    log(f"download: verified sha256={actual}")
    return dest


# ── assembly ──────────────────────────────────────────────────────────────────


def build_frontend() -> Path:
    dist = REPO_ROOT / "frontend" / "dist"
    frontend = REPO_ROOT / "frontend"
    npm = shutil.which("npm") or "npm"
    run([npm, "ci"], cwd=frontend)
    run([npm, "run", "build"], cwd=frontend)
    if not (dist / "index.html").exists():
        raise SystemExit("frontend build did not produce dist/index.html")
    return dist


def build_desktop() -> Path:
    log("desktop: build shared Tauri shell (release, locked)")
    run(["cargo", "build", "--release", "--locked"], cwd=DESKTOP_ROOT / "src-tauri")
    executable = DESKTOP_ROOT / "src-tauri/target/release/dataops-studio-desktop.exe"
    if not executable.exists():
        raise SystemExit(f"desktop build did not produce {executable}")
    return executable


def _server2019_keeps(member: str) -> bool:
    """Whether a slim-set member also survives the server2019 trim."""

    name = Path(member).name.lower()
    if member.startswith("pgsql/bin/"):
        if name.endswith(".exe"):
            return name in SERVER2019_PG_EXES
        return not name.startswith(SERVER2019_PG_DROP_DLL_PREFIXES)
    if member.startswith("pgsql/lib/"):
        # MSVC import libraries are only for compiling extensions.
        return not name.endswith(".lib")
    return True


def extract_pg(zip_path: Path, staging: Path, *, lite: bool = False) -> None:
    target = staging / "pgsql"
    log(f"pg: extract slim server set (bin/lib/share){' + server2019 trim' if lite else ''}")
    keep_prefixes = tuple(f"pgsql/{name}/" for name in PG_KEEP_DIRS)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        members = [m for m in names if m.startswith(keep_prefixes) and not m.endswith("/")]
        if lite:
            members = [m for m in members if _server2019_keeps(m)]
        for member in members:
            rel = Path(member).relative_to("pgsql")
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        # Keep the server license for compliance.
        if "pgsql/server_license.txt" in names:
            target.mkdir(parents=True, exist_ok=True)
            with (
                archive.open("pgsql/server_license.txt") as src,
                (target / "server_license.txt").open("wb") as dst,
            ):
                shutil.copyfileobj(src, dst)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name))


def find_vc_redist_dir(explicit: str | None) -> Path:
    """Locate the x64 ``Microsoft.VC14x.CRT`` app-local redistributable folder.

    An explicit ``--vc-redist-dir`` wins; otherwise the newest MSVC redist of any
    Visual Studio / Build Tools install found by vswhere (or ``VCToolsRedistDir``
    from a developer prompt) is used.
    """

    if explicit:
        return Path(explicit).resolve()
    redist_roots: list[Path] = []
    if os.environ.get("VCToolsRedistDir"):
        redist_roots.append(Path(os.environ["VCToolsRedistDir"]).parent)
    vswhere = (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio/Installer/vswhere.exe"
    )
    if vswhere.exists():
        result = subprocess.run(
            [
                str(vswhere),
                "-all",
                "-prerelease",
                "-products",
                "*",
                "-property",
                "installationPath",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        redist_roots.extend(
            Path(line.strip()) / "VC" / "Redist" / "MSVC"
            for line in result.stdout.splitlines()
            if line.strip()
        )
    candidates = [
        crt
        for root in redist_roots
        for crt in root.glob("*/x64/Microsoft.VC14*.CRT")
        if all((crt / dll).exists() for dll in VC_RUNTIME_DLLS)
    ]
    if not candidates:
        raise SystemExit(
            "VC++ x64 redistributable not found; install the Visual Studio C++ build tools "
            "or pass --vc-redist-dir <...\\x64\\Microsoft.VC14x.CRT>"
        )
    return max(candidates, key=lambda crt: _version_key(crt.parent.parent))


def install_vc_runtime(redist_dir: Path, staging: Path) -> list[tuple[str, str]]:
    """Copy the VC++ runtime next to postgres.exe; return (name, sha256) pairs."""

    pg_bin = staging / "pgsql" / "bin"
    log(f"assemble: VC++ runtime from {redist_dir}")
    copied: list[tuple[str, str]] = []
    for name in VC_RUNTIME_DLLS:
        source = redist_dir / name
        if not source.exists():
            raise SystemExit(f"VC++ redistributable is missing {source}")
        shutil.copy2(source, pg_bin / name)
        copied.append((name, sha256_file(pg_bin / name)))
    return copied


def assemble(
    staging: Path,
    *,
    mode: str,
    requirements: Path,
    frontend_dist: Path,
    python_dir: Path | None = None,
    uv_dir: Path | None = None,
    wheels: Path | None = None,
    pg_zip: Path | None = None,
    desktop_executable: Path | None = None,
    vc_redist_dir: Path | None = None,
    profile: str = "win10",
    identity: BuildIdentity,
) -> list[tuple[str, str]]:
    """Assemble the staging tree; return the bundled VC++ runtime (name, sha256)."""

    vc_runtime: list[tuple[str, str]] = []
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    log("assemble: application source")
    for item in SOURCE_ITEMS:
        src = REPO_ROOT / item
        dst = staging / item
        if src.is_dir():
            _copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    log("assemble: frontend dist")
    _copytree(frontend_dist, staging / "frontend" / "dist")

    runtime = staging / "runtime"
    runtime.mkdir(parents=True)
    shutil.copy2(requirements, runtime / "requirements-frozen.txt")
    write_update_metadata(staging, requirements, identity)

    if mode == "offline":
        assert python_dir and uv_dir and wheels and pg_zip
        log("assemble: standalone python")
        _copytree(python_dir, runtime / "python")
        log("assemble: uv")
        _copytree(uv_dir, runtime / "uv")
        log("assemble: wheels")
        (runtime / "wheels").mkdir()
        for wheel in wheels.glob("*.whl"):
            shutil.copy2(wheel, runtime / "wheels" / wheel.name)
        extract_pg(pg_zip, staging, lite=profile == "server2019")
        if vc_redist_dir is not None:
            vc_runtime = install_vc_runtime(vc_redist_dir, staging)
    else:
        log("assemble: online download-manifest.json (python/uv/pg pins)")
        write_download_manifest(staging)

    if desktop_executable is not None:
        log("assemble: Tauri desktop shell")
        shutil.copy2(desktop_executable, staging / "DataOpsStudio.exe")

    write_scripts(staging, identity)
    write_readme(staging, profile=profile)
    return vc_runtime


def download_manifest() -> dict[str, object]:
    """The URL + sha256 pins install.ps1 uses to provision an online bundle."""

    return {
        "schema": 1,
        "mode": "online",
        "python": {
            "version": PYTHON_FULL_VERSION,
            "url": PYTHON_STANDALONE_URL,
            "sha256": PYTHON_STANDALONE_SHA256,
        },
        "uv": {
            "version": UV_VERSION,
            "url": UV_URL,
            "sha256": UV_SHA256,
        },
        "postgres": {
            "version": PG_VERSION,
            "url": PG_URL,
            "sha256": PG_SHA256,
            "keep": list(PG_KEEP_DIRS),
        },
    }


def write_download_manifest(staging: Path) -> None:
    runtime = staging / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "download-manifest.json").write_text(
        json.dumps(download_manifest(), indent=2) + "\n", encoding="utf-8"
    )


def write_update_metadata(
    staging: Path,
    requirements: Path,
    identity: BuildIdentity,
) -> None:
    compatibility = build_update_compatibility(
        repo_root=REPO_ROOT,
        platform="win10-x64",
        requirements=requirements,
        python={"version": PYTHON_FULL_VERSION, "sha256": PYTHON_STANDALONE_SHA256},
        uv={"version": UV_VERSION, "sha256": UV_SHA256},
        postgres={"version": PG_VERSION, "sha256": PG_SHA256},
    )
    write_shared_update_metadata(
        runtime_dir=staging / "runtime",
        compatibility=compatibility,
        version=identity.version,
        commit=identity.commit,
        image_version=identity.image_version,
    )


def _write_source(source: Path, dest: Path, *, encoding: str) -> None:
    text = source.read_text(encoding="utf-8")
    dest.write_text(text, encoding=encoding, newline="\r\n")


# PowerShell core + one-line .cmd double-click shims (no batch quote nesting).
SCRIPT_TEMPLATES = (
    "_common.ps1",
    "install.ps1",
    "start.ps1",
    "stop.ps1",
    "start.cmd",
    "stop.cmd",
)
BUILD_IDENTITY_MARKER = "# __DATAOPS_BUILD_IDENTITY__"


def write_scripts(staging: Path, identity: BuildIdentity) -> None:
    log("assemble: PowerShell scripts + .cmd shims")
    for name in SCRIPT_TEMPLATES:
        source = SCRIPTS_ROOT / name
        destination = staging / name
        if name == "_common.ps1":
            text = source.read_text(encoding="utf-8")
            destination.write_text(
                inject_build_identity(
                    text,
                    marker=BUILD_IDENTITY_MARKER,
                    rendered_identity=render_powershell_build_identity(identity),
                    keep_shebang=False,
                ),
                encoding="ascii",
                newline="\r\n",
            )
        else:
            _write_source(source, destination, encoding="ascii")


def write_readme(staging: Path, *, profile: str = "win10") -> None:
    text = (TEMPLATES_DIR / "README.md").read_text(encoding="utf-8")
    if profile == "server2019":
        # Server 2019 notes go first; the shared Win10 README still applies.
        server = (TEMPLATES_DIR / "README-server2019.md").read_text(encoding="utf-8")
        text = server + "\n" + text
    (staging / "README.md").write_text(text, encoding="utf-8", newline="\r\n")


def write_manifest(
    staging_parent: Path,
    zip_path: Path,
    mode: str,
    *,
    gui: bool,
    identity: BuildIdentity,
    profile: str = "win10",
    vc_runtime: list[tuple[str, str]] | None = None,
    vc_redist_dir: Path | None = None,
) -> None:
    if profile == "server2019":
        redist = "?"
        if vc_redist_dir is not None:
            redist = f"{vc_redist_dir.parent.parent.name}/{vc_redist_dir.name}"
        runtime_lines = [
            f"postgresql: {PG_VERSION} (EDB windows-x64 binaries, server2019 lite set, bundled)",
            f"python: python-build-standalone {PYTHON_FULL_VERSION} (relocatable, bundled)",
            "wheels: bundled (offline pip install --no-index)",
            f"vc_runtime: MSVC {redist} (app-local in pgsql/bin)",
            *(f"vc_runtime_sha256: {name} {digest}" for name, digest in vc_runtime or []),
            "gui: none (browser access; Server 2019 ships no WebView2)",
        ]
    elif mode == "offline":
        runtime_lines = [
            f"postgresql: {PG_VERSION} (EDB windows-x64 binaries, slim bin/lib/share, bundled)",
            f"python: python-build-standalone {PYTHON_FULL_VERSION} (relocatable, bundled)",
            "wheels: bundled (offline pip install --no-index)",
        ]
    else:
        runtime_lines = [
            f"postgresql: {PG_VERSION} (downloaded on first run, sha256 pinned)",
            f"python: python-build-standalone {PYTHON_FULL_VERSION} (downloaded, sha256 pinned)",
            f"uv: {UV_VERSION} (downloaded, sha256 pinned)",
            "wheels: downloaded from PyPI on first run (--require-hashes)",
        ]
    title = "Windows Server 2019 x64" if profile == "server2019" else "Win10 x64"
    lines = [
        f"DataOpsStudio {title} {mode}{' GUI' if gui else ''} bundle",
        f"bundle_version: {identity.version}",
        f"build_commit: {identity.commit}",
        f"image_version: {identity.image_version}",
        *runtime_lines,
        *(["gui: Tauri v2 desktop shell (WebView2 required)"] if gui else []),
        f"zip: {zip_path.name}",
        f"zip_sha256: {sha256_file(zip_path)}",
        f"zip_size_mb: {zip_path.stat().st_size / 1024 / 1024:.1f}",
    ]
    (staging_parent / f"{zip_path.stem}.MANIFEST.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    for line in lines:
        log(line)
    if gui and mode == "offline" and zip_path.stat().st_size > MAX_GUI_ZIP_BYTES:
        raise SystemExit(
            f"size gate failed: {zip_path.stat().st_size / 1024 / 1024:.2f} MiB exceeds 189 MiB"
        )


def make_zip(staging: Path, output_dir: Path, name: str) -> Path:
    zip_path = output_dir / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    log(f"zip: writing {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, Path(name) / path.relative_to(staging))
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Win10 x64 deployment bundle.")
    parser.add_argument(
        "--mode",
        choices=("offline", "online"),
        default="offline",
        help="offline: zip embeds runtime+wheels+PG; online: light zip, download on target",
    )
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "dist-bundle"))
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / "dist-bundle" / "cache"))
    parser.add_argument(
        "--build-commit",
        default=None,
        help="full Git object id; required when building from a source archive without .git",
    )
    parser.add_argument("--uv", default="uv", help="uv executable on the build host")
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="keep the uncompressed staging directory after zipping",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="build the shared Tauri shell and include DataOpsStudio.exe",
    )
    parser.add_argument(
        "--profile",
        choices=("win10", "server2019"),
        default="win10",
        help="server2019: offline script-only lite bundle with app-local VC++ runtime",
    )
    parser.add_argument(
        "--vc-redist-dir",
        default=None,
        help="x64 Microsoft.VC14x.CRT folder for --profile server2019 (default: vswhere newest)",
    )
    args = parser.parse_args(argv)
    if args.profile == "server2019" and (args.gui or args.mode != "offline"):
        parser.error("--profile server2019 is offline-only and has no GUI")

    if os.name != "nt":
        log("WARNING: not running on Windows; wheels/python will not match win_amd64")

    output_dir = Path(args.output_dir).resolve()
    cache = Path(args.cache_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    version = bundle_version()
    if args.profile == "server2019":
        name = f"dataops-studio-{version}-server2019-x64-offline"
    elif args.gui and args.mode == "offline":
        name = f"dataops-studio-{version}-win10-x64-gui"
    elif args.gui:
        name = f"dataops-studio-{version}-win10-x64-{args.mode}-gui"
    else:
        name = f"dataops-studio-{version}-win10-x64-{args.mode}"
    identity = resolve_build_identity(
        REPO_ROOT,
        explicit_commit=args.build_commit,
        image_version=name.removeprefix("dataops-studio-"),
    )
    log(f"build identity: version={identity.version} commit={identity.commit}")
    vc_redist_dir = find_vc_redist_dir(args.vc_redist_dir) if args.profile == "server2019" else None

    requirements = export_requirements(cache, args.uv)
    frontend_dist = build_frontend()
    desktop_executable = build_desktop() if args.gui else None

    staging = output_dir / name
    vc_runtime: list[tuple[str, str]] = []
    if args.mode == "offline":
        python_dir = prepare_python(cache)
        uv_dir = prepare_uv(cache)
        wheels = download_wheels(cache, python_dir, requirements)
        pg_zip = download_pg(cache)
        vc_runtime = assemble(
            staging,
            mode="offline",
            requirements=requirements,
            frontend_dist=frontend_dist,
            python_dir=python_dir,
            uv_dir=uv_dir,
            wheels=wheels,
            pg_zip=pg_zip,
            desktop_executable=desktop_executable,
            vc_redist_dir=vc_redist_dir,
            profile=args.profile,
            identity=identity,
        )
    else:
        assemble(
            staging,
            mode="online",
            requirements=requirements,
            frontend_dist=frontend_dist,
            desktop_executable=desktop_executable,
            identity=identity,
        )

    zip_path = make_zip(staging, output_dir, name)
    write_manifest(
        output_dir,
        zip_path,
        args.mode,
        gui=args.gui,
        identity=identity,
        profile=args.profile,
        vc_runtime=vc_runtime,
        vc_redist_dir=vc_redist_dir,
    )
    if not args.keep_staging:
        shutil.rmtree(staging)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
