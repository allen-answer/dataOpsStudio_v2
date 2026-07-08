"""Build a fully offline Win10 x64 deployment bundle for DataOpsStudio 2.0.

The produced ``dataops-studio-<ver>-win10-x64-offline.zip`` is self-contained: an
air-gapped Windows 10 x64 host unzips it and runs ``start.bat`` — no Python, no
network, no separate PostgreSQL install required. The bundle carries

* a relocatable standalone CPython 3.12 (python-build-standalone, via ``uv``);
* ``uv.exe`` (drives the launcher's ``uv run`` child processes offline);
* every runtime wheel (incl. psycopg[binary], pymysql, oracledb thin, ibm_db,
  dmPython) for cp312 win_amd64 — offline ``pip install --no-index`` installable;
* PostgreSQL 16 Windows x64 server binaries (bin/lib/share, EDB zip, slimmed —
  pgAdmin/StackBuilder/doc/include dropped);
* the built Vue SPA (``frontend/dist``);
* the application source + ``uv.lock`` (reproducible env) + first-run scripts.

Zero secrets are ever written into the bundle (R8). The bootstrap master key,
PG passwords and admin password are generated/entered on the target host at
first run, never in git and never in this package.

DM (达梦) note: the dmPython *driver wheel* is bundled (it is a locked main
dependency and ships a cp312 win_amd64 wheel), but the proprietary DM *client*
(DM_HOME crypto libraries) is NOT — real DM connectivity needs a separate DM
client install, see docs/deployment/win10-offline-bundle.md.

Usage (build host needs network + uv + Node/npm)::

    uv run python tools/package/build_win10_bundle.py
    uv run python tools/package/build_win10_bundle.py --skip-frontend-build

Large artifacts land in ``dist-bundle/`` (git-ignored); the bundle itself never
enters git.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Pinned downstream artifacts. Recorded (not silently trusted): the script
# prints the sha256 it actually saw so a reviewer can compare across rebuilds.
PG_VERSION = "16.14-1"
PG_URL = (
    "https://get.enterprisedb.com/postgresql/"
    f"postgresql-{PG_VERSION}-windows-x64-binaries.zip"
)
PG_SHA256 = "98af1417ba6a8dc30543e560e5407833a3b9e7cc7ed20e73b2006f3aa2f04663"
PYTHON_VERSION = "3.12"
# PG components to keep (server-only slim set). pgAdmin (~670MB), StackBuilder,
# doc and include are dropped — the launcher only needs initdb/pg_ctl/postgres.
PG_KEEP_DIRS = ("bin", "lib", "share")

# Application source copied into the bundle (everything `python -m app.*` and
# `alembic upgrade head` need at runtime, cwd = bundle root).
SOURCE_ITEMS = (
    "app",
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


def prepare_python(cache: Path, uv: str) -> Path:
    """Return a relocatable standalone CPython 3.12 dir, cached in the bundle cache.

    Uses uv to fetch/locate a python-build-standalone interpreter (the same
    relocatable build uv ships), then snapshots it into the cache so the build
    is stable even if the uv-managed copy later changes.
    """

    dest = cache / "python"
    if (dest / "python.exe").exists():
        log(f"python: reuse cached {dest}")
        return dest
    run([uv, "python", "install", PYTHON_VERSION])
    managed = _find_standalone_python(uv)
    log(f"python: snapshot standalone interpreter from {managed}")
    _copytree(managed, dest)
    return dest


def _find_standalone_python(uv: str) -> Path:
    """Locate a standalone (relocatable) CPython install dir from uv's inventory.

    `uv python find` prefers a project/active venv, which is not relocatable and
    lacks a full base install; parse `uv python list` and pick a real managed
    install instead (has python.exe + Lib/ at its root, and is not a venv).
    """

    output = subprocess.run(
        [uv, "python", "list", "--only-installed"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(f"cpython-{PYTHON_VERSION}"):
            continue
        exe = Path(parts[-1])
        root = exe.parent
        if "venv" in {p.lower() for p in exe.parts} or root.name.lower() == "scripts":
            continue
        if exe.name.lower() == "python.exe" and (root / "Lib").is_dir():
            return root
    raise SystemExit(
        f"no standalone CPython {PYTHON_VERSION} install found; run "
        f"`uv python install {PYTHON_VERSION}` and retry"
    )


def prepare_uv(cache: Path, uv: str) -> Path:
    dest = cache / "uv"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "uv.exe"
    source = shutil.which(uv)
    if source is None:
        raise SystemExit(f"cannot locate uv executable on PATH: {uv}")
    if not target.exists():
        log(f"uv: copy {source} -> {target}")
        shutil.copy2(source, target)
    return dest


def download_wheels(cache: Path, python_dir: Path, requirements: Path) -> Path:
    wheels = cache / "wheels"
    if wheels.exists() and any(wheels.glob("*.whl")):
        log(f"wheels: reuse cached {wheels} ({len(list(wheels.glob('*.whl')))} files)")
        return wheels
    wheels.mkdir(parents=True, exist_ok=True)
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
    return wheels


def download_pg(cache: Path) -> Path:
    zip_path = cache / "pg16.zip"
    if not zip_path.exists():
        log(f"pg: download {PG_URL}")
        _download(PG_URL, zip_path)
    digest = sha256_file(zip_path)
    log(f"pg: sha256={digest}")
    if digest != PG_SHA256:
        log(f"WARNING: PG zip sha256 mismatch (expected {PG_SHA256})")
    return zip_path


def _download(url: str, dest: Path) -> None:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as response:
        with tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    tmp.replace(dest)


# ── assembly ──────────────────────────────────────────────────────────────────


def build_frontend(skip: bool) -> Path:
    dist = REPO_ROOT / "frontend" / "dist"
    if skip:
        if not (dist / "index.html").exists():
            raise SystemExit("--skip-frontend-build set but frontend/dist/index.html missing")
        log(f"frontend: reuse existing {dist}")
        return dist
    frontend = REPO_ROOT / "frontend"
    npm = shutil.which("npm") or "npm"
    run([npm, "ci"], cwd=frontend)
    run([npm, "run", "build"], cwd=frontend)
    if not (dist / "index.html").exists():
        raise SystemExit("frontend build did not produce dist/index.html")
    return dist


def extract_pg(zip_path: Path, staging: Path) -> None:
    target = staging / "pgsql"
    log("pg: extract slim server set (bin/lib/share)")
    keep_prefixes = tuple(f"pgsql/{name}/" for name in PG_KEEP_DIRS)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        members = [m for m in names if m.startswith(keep_prefixes) and not m.endswith("/")]
        for member in members:
            rel = Path(member).relative_to("pgsql")
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        # Keep the server license for compliance.
        if "pgsql/server_license.txt" in names:
            target.mkdir(parents=True, exist_ok=True)
            with archive.open("pgsql/server_license.txt") as src, (
                target / "server_license.txt"
            ).open("wb") as dst:
                shutil.copyfileobj(src, dst)


def assemble(
    staging: Path,
    *,
    python_dir: Path,
    uv_dir: Path,
    wheels: Path,
    requirements: Path,
    frontend_dist: Path,
    pg_zip: Path,
) -> None:
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
    log("assemble: standalone python")
    _copytree(python_dir, runtime / "python")
    log("assemble: uv")
    _copytree(uv_dir, runtime / "uv")
    log("assemble: wheels")
    (runtime / "wheels").mkdir()
    for wheel in wheels.glob("*.whl"):
        shutil.copy2(wheel, runtime / "wheels" / wheel.name)
    shutil.copy2(requirements, runtime / "requirements-frozen.txt")

    extract_pg(pg_zip, staging)

    write_scripts(staging)
    write_readme(staging)


def _write_template(name: str, dest: Path, *, encoding: str) -> None:
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    dest.write_text(text, encoding=encoding, newline="\r\n")


def write_scripts(staging: Path) -> None:
    log("assemble: start.bat / stop.bat")
    _write_template("start.bat", staging / "start.bat", encoding="ascii")
    _write_template("stop.bat", staging / "stop.bat", encoding="ascii")


def write_readme(staging: Path) -> None:
    _write_template("README.md", staging / "README.md", encoding="utf-8")


def write_manifest(staging_parent: Path, zip_path: Path) -> None:
    lines = [
        "DataOpsStudio Win10 x64 offline bundle",
        f"bundle_version: {bundle_version()}",
        f"postgresql: {PG_VERSION} (EDB windows-x64 binaries, slim bin/lib/share)",
        f"python: python-build-standalone {PYTHON_VERSION} (relocatable, offline)",
        f"zip: {zip_path.name}",
        f"zip_sha256: {sha256_file(zip_path)}",
        f"zip_size_mb: {zip_path.stat().st_size / 1024 / 1024:.1f}",
    ]
    (staging_parent / f"{zip_path.stem}.MANIFEST.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    for line in lines:
        log(line)


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
    parser = argparse.ArgumentParser(description="Build the Win10 x64 offline bundle.")
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "dist-bundle"))
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / "dist-bundle" / "cache"))
    parser.add_argument("--uv", default="uv", help="uv executable on the build host")
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="reuse existing frontend/dist instead of running npm ci && npm run build",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="keep the uncompressed staging directory after zipping",
    )
    args = parser.parse_args(argv)

    if os.name != "nt":
        log("WARNING: not running on Windows; wheels/python will not match win_amd64")

    output_dir = Path(args.output_dir).resolve()
    cache = Path(args.cache_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    version = bundle_version()
    name = f"dataops-studio-{version}-win10-x64-offline"

    requirements = export_requirements(cache, args.uv)
    python_dir = prepare_python(cache, args.uv)
    uv_dir = prepare_uv(cache, args.uv)
    wheels = download_wheels(cache, python_dir, requirements)
    pg_zip = download_pg(cache)
    frontend_dist = build_frontend(args.skip_frontend_build)

    staging = output_dir / name
    assemble(
        staging,
        python_dir=python_dir,
        uv_dir=uv_dir,
        wheels=wheels,
        requirements=requirements,
        frontend_dist=frontend_dist,
        pg_zip=pg_zip,
    )

    zip_path = make_zip(staging, output_dir, name)
    write_manifest(output_dir, zip_path)
    if not args.keep_staging:
        shutil.rmtree(staging)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
