"""Prepare and finalize signed app/frontend-only desktop update packages.

Private keys never enter this tool. ``prepare_payload_update`` emits canonical
``manifest.json`` bytes for the release system to sign.  ``finalize_payload_update``
accepts only the detached signature and key id, then creates the importable package.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

if __package__:
    from .build_identity import BuildIdentity, resolve_build_identity
    from .update_compatibility import build_update_compatibility, sha256_file
else:
    from build_identity import BuildIdentity, resolve_build_identity
    from update_compatibility import build_update_compatibility, sha256_file

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PAYLOAD_ROOT = "payload"
_MANIFEST_NAME = "manifest.json"
_SIGNATURE_NAME = "signature.json"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _artifact(compatibility: dict[str, object], name: str) -> dict[str, str]:
    value = compatibility.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"base compatibility is missing {name}")
    version = value.get("version")
    sha256 = value.get("sha256")
    if not isinstance(version, str) or not isinstance(sha256, str):
        raise ValueError(f"base compatibility has invalid {name}")
    return {"version": version, "sha256": sha256}


def _copytree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"payload source must not be a symlink: {path}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _payload_files(payload: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    seen: set[str] = set()
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"payload source must not be a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(payload).as_posix()
        folded = relative.casefold()
        if folded in seen:
            raise ValueError(f"payload paths must be case-insensitively unique: {relative}")
        seen.add(folded)
        files.append({"path": relative, "sha256": sha256_file(path), "size": path.stat().st_size})
    if not any(item["path"] == "app/__init__.py" for item in files):
        raise ValueError("payload must contain app/__init__.py")
    if not any(item["path"] == "frontend/dist/index.html" for item in files):
        raise ValueError("payload must contain frontend/dist/index.html")
    return files


def prepare_payload_update(
    *,
    repo_root: Path,
    frontend_dist: Path,
    requirements: Path,
    base_compatibility: dict[str, object],
    identity: BuildIdentity,
    work_dir: Path,
) -> Path:
    """Create payload files plus the exact manifest bytes to sign."""

    if work_dir.exists():
        raise FileExistsError(f"update work directory already exists: {work_dir}")
    platform = base_compatibility.get("platform")
    if not isinstance(platform, str):
        raise ValueError("base compatibility is missing platform")
    current = build_update_compatibility(
        repo_root=repo_root,
        platform=platform,
        requirements=requirements,
        python=_artifact(base_compatibility, "python"),
        uv=_artifact(base_compatibility, "uv"),
        postgres=_artifact(base_compatibility, "postgres"),
    )
    if current != base_compatibility:
        raise ValueError("runtime, wheels, PostgreSQL, Tauri, migrations, or updater changed")
    if _SAFE_VALUE.fullmatch(identity.version) is None:
        raise ValueError("invalid payload version")
    if _GIT_COMMIT.fullmatch(identity.commit) is None:
        raise ValueError("invalid payload commit")
    if _SAFE_VALUE.fullmatch(identity.image_version) is None:
        raise ValueError("invalid payload image version")

    payload = work_dir / _PAYLOAD_ROOT
    payload.mkdir(parents=True)
    _copytree(repo_root / "app", payload / "app")
    _copytree(frontend_dist, payload / "frontend/dist")
    manifest = {
        "schema_version": 1,
        "kind": "dataops-payload-update",
        "release": {
            "version": identity.version,
            "commit": identity.commit,
            "image_version": identity.image_version,
        },
        "compatibility": base_compatibility,
        "files": _payload_files(payload),
    }
    manifest_path = work_dir / _MANIFEST_NAME
    manifest_path.write_bytes(_canonical_json(manifest))
    return manifest_path


def _read_signature(path: Path) -> str:
    encoded = path.read_text(encoding="ascii").strip()
    try:
        signature = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("signature file must contain strict base64") from exc
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    return encoded


def _verify_work_tree(work_dir: Path, manifest: dict[str, Any]) -> None:
    payload = work_dir / _PAYLOAD_ROOT
    expected = manifest.get("files")
    if not isinstance(expected, list) or _payload_files(payload) != expected:
        raise ValueError("payload files no longer match manifest.json")


def finalize_payload_update(
    *,
    work_dir: Path,
    key_id: str,
    signature_file: Path,
    output: Path,
) -> Path:
    """Combine a prepared payload with an externally produced detached signature."""

    if _SAFE_VALUE.fullmatch(key_id) is None:
        raise ValueError("invalid update signing key id")
    if output.exists():
        raise FileExistsError(f"update package already exists: {output}")
    manifest_path = work_dir / _MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest_bytes != _canonical_json(manifest):
        raise ValueError("manifest.json is not canonical")
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must contain an object")
    _verify_work_tree(work_dir, manifest)
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": _read_signature(signature_file),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_MANIFEST_NAME, manifest_bytes)
        archive.writestr(_SIGNATURE_NAME, _canonical_json(signature))
        for path in sorted((work_dir / _PAYLOAD_ROOT).rglob("*")):
            if path.is_file():
                relative = path.relative_to(work_dir / _PAYLOAD_ROOT).as_posix()
                archive.write(path, f"{_PAYLOAD_ROOT}/{relative}")
    return output


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _build_frontend(repo_root: Path) -> Path:
    frontend = repo_root / "frontend"
    npm = shutil.which("npm") or "npm"
    _run([npm, "ci"], cwd=frontend)
    _run([npm, "run", "build"], cwd=frontend)
    destination = frontend / "dist"
    if not (destination / "index.html").is_file():
        raise SystemExit("frontend build did not produce frontend/dist/index.html")
    return destination


def _export_requirements(repo_root: Path, *, uv: str, platform: str, output: Path) -> Path:
    command = [
        uv,
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--format",
        "requirements-txt",
    ]
    if platform == "win10-x64":
        command.extend(("--extra", "oracle", "--extra", "db2"))
    command.extend(("-o", str(output)))
    _run(command, cwd=repo_root)
    return output


def _load_compatibility(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("base compatibility must contain a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="build payload and canonical manifest to sign")
    prepare.add_argument("--platform", choices=("win10-x64", "mint21.3-x86_64"), required=True)
    prepare.add_argument("--base-compatibility", required=True)
    prepare.add_argument("--output-dir", default=str(REPO_ROOT / "dist-update"))
    prepare.add_argument("--build-commit", default=None)
    prepare.add_argument("--uv", default="uv")

    finalize = commands.add_parser(
        "finalize", help="attach an external signature and write package"
    )
    finalize.add_argument("--work-dir", required=True)
    finalize.add_argument("--key-id", required=True)
    finalize.add_argument("--signature-file", required=True)
    finalize.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "finalize":
        package = finalize_payload_update(
            work_dir=Path(args.work_dir).resolve(),
            key_id=str(args.key_id),
            signature_file=Path(args.signature_file).resolve(),
            output=Path(args.output).resolve(),
        )
        print(f"payload update package: {package}")
        print(f"sha256: {sha256_file(package)}")
        return 0

    base_compatibility = _load_compatibility(Path(args.base_compatibility).resolve())
    output_dir = Path(args.output_dir).resolve()
    platform = str(args.platform)
    identity = resolve_build_identity(
        REPO_ROOT,
        explicit_commit=args.build_commit,
        image_version=f"payload-{platform}",
    )
    identity = BuildIdentity(
        version=identity.version,
        commit=identity.commit,
        image_version=f"{identity.version}-{platform}-payload",
    )
    work_dir = output_dir / f"dataops-studio-{identity.version}-{platform}-payload.work"
    if work_dir.exists():
        raise SystemExit(f"update work directory already exists: {work_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    requirements = _export_requirements(
        REPO_ROOT,
        uv=str(args.uv),
        platform=platform,
        output=output_dir / f"requirements-{platform}.txt",
    )
    frontend_dist = _build_frontend(REPO_ROOT)
    manifest = prepare_payload_update(
        repo_root=REPO_ROOT,
        frontend_dist=frontend_dist,
        requirements=requirements,
        base_compatibility=base_compatibility,
        identity=identity,
        work_dir=work_dir,
    )
    print(f"manifest to sign: {manifest}")
    print(f"manifest sha256: {sha256_file(manifest)}")
    print("sign manifest.json externally, then run the finalize subcommand")
    return 0


__all__ = ["finalize_payload_update", "prepare_payload_update"]


if __name__ == "__main__":
    raise SystemExit(main())
