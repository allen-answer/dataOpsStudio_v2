"""Verify, stage and atomically select signed app/frontend payloads."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from app.infrastructure.license.signatures import verify_ed25519_signature

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_SIGNATURE_BYTES = 4 * 1024
_MAX_TRUST_STORE_BYTES = 1024 * 1024
_MAX_FILES = 20_000
_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
_RUNTIME_SUPPORT_FILES = ("alembic.ini", "pyproject.toml", "uv.lock", ".python-version")
_WINDOWS_INVALID_PATH_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    version: str
    commit: str
    image_version: str


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    root: Path
    release: ReleaseIdentity | None


@dataclass(frozen=True, slots=True)
class UpdateReceipt:
    release: ReleaseIdentity
    previous: ReleaseIdentity | None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_json_bytes(data: bytes, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain an object")
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _decode_base64(value: object, *, name: str, size: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if len(decoded) != size:
        raise ValueError(f"invalid {name}")
    return decoded


def _release(value: object) -> ReleaseIdentity:
    if not isinstance(value, dict) or set(value) != {"version", "commit", "image_version"}:
        raise ValueError("invalid update release identity")
    version = value["version"]
    commit = value["commit"]
    image_version = value["image_version"]
    if not isinstance(version, str) or _SAFE_VALUE.fullmatch(version) is None:
        raise ValueError("invalid update version")
    if not isinstance(commit, str) or _GIT_COMMIT.fullmatch(commit) is None:
        raise ValueError("invalid update commit")
    if not isinstance(image_version, str) or _SAFE_VALUE.fullmatch(image_version) is None:
        raise ValueError("invalid update image version")
    return ReleaseIdentity(version=version, commit=commit, image_version=image_version)


def _release_dict(release: ReleaseIdentity) -> dict[str, str]:
    return {
        "version": release.version,
        "commit": release.commit,
        "image_version": release.image_version,
    }


def _release_id(release: ReleaseIdentity) -> str:
    return f"{release.version}--{release.commit}"


def _sha256_stream(source: Any, destination: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        if size > _MAX_PAYLOAD_BYTES:
            raise ValueError("update payload exceeds size limit")
        digest.update(chunk)
        destination.write(chunk)
    return digest.hexdigest(), size


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    module: Any
    try:
        if os.name == "nt":
            module = importlib.import_module("msvcrt")
            module.locking(handle.fileno(), module.LK_NBLCK, 1)
        else:
            module = importlib.import_module("fcntl")
            module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ValueError("another update transaction is active") from exc
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            module.locking(handle.fileno(), module.LK_UNLCK, 1)
        else:
            module.flock(handle.fileno(), module.LOCK_UN)
        handle.close()


def _unsafe_windows_component(part: str) -> bool:
    normalized = part.rstrip(" .")
    stem = normalized.split(".", 1)[0].casefold()
    return (
        normalized != part
        or any(character in _WINDOWS_INVALID_PATH_CHARS for character in part)
        or stem in _WINDOWS_RESERVED_NAMES
    )


class PayloadUpdater:
    """The single public seam for payload selection and update transactions."""

    def __init__(self, *, bundle_root: Path, state_root: Path) -> None:
        self.bundle_root = bundle_root.resolve()
        self.state_root = state_root.resolve()
        self.update_root = self.state_root / "updates"
        self.versions_root = self.update_root / "versions"
        self.current_path = self.update_root / "current.json"
        self.pending_path = self.update_root / "pending.json"
        self.rollback_path = self.update_root / "rollback.json"
        self.transaction_lock_path = self.update_root / "transaction.lock"
        self.runtime_lock_path = self.update_root / "runtime.lock"

    def _installed_compatibility(self) -> dict[str, Any]:
        path = self.bundle_root / "runtime/update-compatibility.json"
        try:
            return _read_json_bytes(path.read_bytes(), name="installed compatibility")
        except OSError as exc:
            raise ValueError("installed package does not support payload updates") from exc

    def _trusted_key(self, trust_store: Path, key_id: object) -> bytes:
        if not isinstance(key_id, str) or _SAFE_VALUE.fullmatch(key_id) is None:
            raise ValueError("invalid update key id")
        if trust_store.stat().st_size > _MAX_TRUST_STORE_BYTES:
            raise ValueError("update trust store exceeds size limit")
        store = _read_json_bytes(trust_store.read_bytes(), name="update trust store")
        if store.get("schema_version") != 1 or not isinstance(store.get("keys"), list):
            raise ValueError("invalid update trust store")
        matches = [
            key for key in store["keys"] if isinstance(key, dict) and key.get("key_id") == key_id
        ]
        if len(matches) != 1 or matches[0].get("algorithm") != "ed25519":
            raise ValueError("update signing key is not trusted")
        return _decode_base64(matches[0].get("public_key"), name="update public key", size=32)

    def _verify_package(
        self, package: Path, trust_store: Path
    ) -> tuple[zipfile.ZipFile, dict[str, Any], bytes, ReleaseIdentity]:
        archive = zipfile.ZipFile(package)
        try:
            infos = archive.infolist()
            if len(infos) > _MAX_FILES + 2:
                raise ValueError("update package contains too many entries")
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("update package contains duplicate paths")
            if "manifest.json" not in names or "signature.json" not in names:
                raise ValueError("update package is missing signed metadata")
            if archive.getinfo("manifest.json").file_size > _MAX_MANIFEST_BYTES:
                raise ValueError("update manifest exceeds size limit")
            if archive.getinfo("signature.json").file_size > _MAX_SIGNATURE_BYTES:
                raise ValueError("update signature metadata exceeds size limit")
            manifest_bytes = archive.read("manifest.json")
            manifest = _read_json_bytes(manifest_bytes, name="update manifest")
            if manifest_bytes != _canonical_json(manifest):
                raise ValueError("update manifest is not canonical")
            signature = _read_json_bytes(archive.read("signature.json"), name="update signature")
            if signature.get("schema_version") != 1 or signature.get("algorithm") != "ed25519":
                raise ValueError("unsupported update signature")
            public_key = self._trusted_key(trust_store, signature.get("key_id"))
            signature_bytes = _decode_base64(
                signature.get("signature"), name="update signature", size=64
            )
            verify_ed25519_signature(
                public_key=public_key,
                signature=signature_bytes,
                message=manifest_bytes,
            )
            if (
                manifest.get("schema_version") != 1
                or manifest.get("kind") != "dataops-payload-update"
            ):
                raise ValueError("unsupported update manifest")
            if manifest.get("compatibility") != self._installed_compatibility():
                raise ValueError(
                    "update requires a full package because runtime compatibility changed"
                )
            release = _release(manifest.get("release"))
            return archive, manifest, manifest_bytes, release
        except Exception:
            archive.close()
            raise

    def _extract(
        self, archive: zipfile.ZipFile, manifest: dict[str, Any], destination: Path
    ) -> None:
        files = manifest.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= _MAX_FILES:
            raise ValueError("invalid update file list")
        expected_names = {"manifest.json", "signature.json"}
        total_size = 0
        seen: set[str] = set()
        has_app = False
        has_frontend = False
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
                raise ValueError("invalid update file record")
            path = item["path"]
            sha256 = item["sha256"]
            size = item["size"]
            if not isinstance(path, str):
                raise ValueError("invalid or duplicate update path")
            pure = PurePosixPath(path)
            folded = path.casefold()
            if folded in seen:
                raise ValueError("invalid or duplicate update path")
            if pure.is_absolute() or pure.as_posix() != path or ".." in pure.parts or "\\" in path:
                raise ValueError("unsafe update path")
            if any(_unsafe_windows_component(part) for part in pure.parts):
                raise ValueError("unsafe update path")
            if path.startswith("app/"):
                has_app = True
            elif path.startswith("frontend/dist/"):
                has_frontend = True
            else:
                raise ValueError("payload may contain only app/ and frontend/dist/")
            if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
                raise ValueError("invalid update file sha256")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("invalid update file size")
            total_size += size
            if total_size > _MAX_PAYLOAD_BYTES:
                raise ValueError("update payload exceeds size limit")
            seen.add(folded)
            archive_name = f"payload/{path}"
            expected_names.add(archive_name)
            info = archive.getinfo(archive_name)
            unix_mode = info.external_attr >> 16
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                raise ValueError("update payload must not contain symlinks")
            destination_root = destination.resolve()
            output = (destination / Path(*pure.parts)).resolve(strict=False)
            if not output.is_relative_to(destination_root):
                raise ValueError("unsafe update path")
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as target:
                actual_sha256, actual_size = _sha256_stream(source, target)
            if actual_size != size or actual_sha256 != sha256:
                raise ValueError(f"update file verification failed: {path}")
        if not has_app or not has_frontend:
            raise ValueError("payload must contain app/ and frontend/dist/")
        actual_names = {item.filename for item in archive.infolist() if not item.is_dir()}
        if actual_names != expected_names:
            raise ValueError("update package contains unmanifested files")

    def _current_record(self) -> dict[str, Any]:
        if not self.current_path.exists():
            return {"schema_version": 1, "kind": "base"}
        value = _read_json_bytes(self.current_path.read_bytes(), name="active update pointer")
        kind = value.get("kind")
        if value.get("schema_version") != 1 or kind not in {"base", "payload"}:
            raise ValueError("invalid active update pointer")
        if kind == "base" and set(value) != {"schema_version", "kind"}:
            raise ValueError("invalid base update pointer")
        if kind == "payload":
            release = _release(value.get("release"))
            if value.get("release_id") != _release_id(release):
                raise ValueError("invalid payload update pointer")
        return value

    def _recover_incomplete_pending(
        self,
        *,
        release: ReleaseIdentity,
        manifest_bytes: bytes,
        signature_bytes: bytes,
    ) -> None:
        if not self.pending_path.exists():
            return
        pending = _read_json_bytes(self.pending_path.read_bytes(), name="pending update")
        if (
            set(pending) != {"schema_version", "previous", "target"}
            or pending.get("schema_version") != 1
        ):
            raise ValueError("invalid pending update")
        previous = pending["previous"]
        target = pending["target"]
        if not isinstance(previous, dict) or not isinstance(target, dict):
            raise ValueError("invalid pending update pointers")
        self._selection(previous)
        target_selection = self._selection(target)
        current = self._current_record()
        if current != previous:
            raise ValueError("an update is already pending health verification")
        if target.get("kind") != "payload":
            raise ValueError("invalid pending update target")
        if target.get("release_id") != _release_id(release):
            raise ValueError("pending update belongs to a different signed release")
        try:
            same_release = (
                target_selection.root.parent / "manifest.json"
            ).read_bytes() == manifest_bytes and (
                target_selection.root.parent / "signature.json"
            ).read_bytes() == signature_bytes
        except OSError:
            same_release = False
        if not same_release:
            raise ValueError("pending update belongs to a different signed release")
        shutil.rmtree(target_selection.root.parent)
        self.pending_path.unlink()

    def _rollback_references(self, release_id: str) -> bool:
        if not self.rollback_path.exists():
            return False
        rollback = _read_json_bytes(self.rollback_path.read_bytes(), name="update rollback")
        if set(rollback) != {"schema_version", "from", "to"} or rollback.get("schema_version") != 1:
            raise ValueError("invalid update rollback state")
        records = (rollback["from"], rollback["to"])
        if not all(isinstance(record, dict) for record in records):
            raise ValueError("invalid update rollback pointers")
        for value in records:
            record = cast(dict[str, Any], value)
            self._selection(record)
            if record.get("kind") == "payload" and record.get("release_id") == release_id:
                return True
        return False

    def _selection(self, record: dict[str, Any]) -> RuntimeSelection:
        if record["kind"] == "base":
            return RuntimeSelection(root=self.bundle_root, release=None)
        release = _release(record["release"])
        root = self.versions_root / str(record["release_id"]) / "payload"
        if not root.is_dir():
            raise ValueError("active update payload is missing")
        return RuntimeSelection(root=root, release=release)

    def _resolve_active_unlocked(self) -> RuntimeSelection:
        return self._selection(self._current_record())

    def resolve_active(self) -> RuntimeSelection:
        with _file_lock(self.transaction_lock_path):
            return self._resolve_active_unlocked()

    def _launcher_environment(
        self,
        selected: RuntimeSelection,
        base: dict[str, str] | None,
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        existing_pythonpath = environment.get("PYTHONPATH")
        python_paths = [str(selected.root)]
        if existing_pythonpath:
            python_paths.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        environment["DATAOPS_FRONTEND_DIST"] = str(selected.root / "frontend/dist")
        release = selected.release
        if release is None:
            identity_path = self.bundle_root / "runtime/build-identity.json"
            if identity_path.exists():
                release = _release(
                    _read_json_bytes(identity_path.read_bytes(), name="installed build identity")
                )
        if release is not None:
            environment["DATAOPS_BUILD_VERSION"] = release.version
            environment["DATAOPS_BUILD_COMMIT"] = release.commit
            environment["DATAOPS_IMAGE_VERSION"] = release.image_version
        return environment

    def launcher_environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return one coherent snapshot of the selected app/frontend identity."""

        with _file_lock(self.transaction_lock_path):
            selected = self._resolve_active_unlocked()
            return self._launcher_environment(selected, base)

    def run_launcher(
        self,
        *,
        python: Path,
        home: Path,
        pg_bin_dir: Path,
        uv: Path,
        arguments: list[str],
    ) -> int:
        """Run app.launcher from the selected payload without shell interpolation."""

        concurrent_runtime_command = bool(arguments) and arguments[0] in {
            "pg-status",
            "status",
            "stop",
        }
        if concurrent_runtime_command:
            return self._run_launcher_unlocked(
                python=python,
                home=home,
                pg_bin_dir=pg_bin_dir,
                uv=uv,
                arguments=arguments,
            )
        with _file_lock(self.runtime_lock_path):
            return self._run_launcher_unlocked(
                python=python,
                home=home,
                pg_bin_dir=pg_bin_dir,
                uv=uv,
                arguments=arguments,
            )

    def _run_launcher_unlocked(
        self,
        *,
        python: Path,
        home: Path,
        pg_bin_dir: Path,
        uv: Path,
        arguments: list[str],
    ) -> int:
        with _file_lock(self.transaction_lock_path):
            selected = self._resolve_active_unlocked()
            environment = self._launcher_environment(selected, None)
        command = [
            str(python),
            "-m",
            "app.launcher",
            "--root",
            str(home),
            "--pg-bin-dir",
            str(pg_bin_dir),
            "--uv",
            str(uv),
            *arguments,
        ]
        result = subprocess.run(
            command,
            cwd=selected.root,
            env=environment,
            check=False,
        )
        return result.returncode

    def install(self, *, package: Path, trust_store: Path) -> UpdateReceipt:
        with _file_lock(self.runtime_lock_path):
            with _file_lock(self.transaction_lock_path):
                return self._install_unlocked(package=package, trust_store=trust_store)

    def _install_unlocked(self, *, package: Path, trust_store: Path) -> UpdateReceipt:
        archive, manifest, manifest_bytes, release = self._verify_package(package, trust_store)
        incoming_signature = archive.read("signature.json")
        try:
            self._recover_incomplete_pending(
                release=release,
                manifest_bytes=manifest_bytes,
                signature_bytes=incoming_signature,
            )
        except Exception:
            archive.close()
            raise
        previous_record = self._current_record()
        previous = self._selection(previous_record).release
        self.versions_root.mkdir(parents=True, exist_ok=True)
        staging_root = self.update_root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="payload-", dir=staging_root))
        release_id = _release_id(release)
        target = self.versions_root / release_id
        if target.exists():
            current_references_target = (
                previous_record.get("kind") == "payload"
                and previous_record.get("release_id") == release_id
            )
            if current_references_target or self._rollback_references(release_id):
                archive.close()
                shutil.rmtree(temporary)
                raise ValueError("update release is already installed")
            try:
                same_release = (target / "manifest.json").read_bytes() == manifest_bytes and (
                    target / "signature.json"
                ).read_bytes() == incoming_signature
            except OSError:
                same_release = False
            if not same_release:
                archive.close()
                shutil.rmtree(temporary)
                raise ValueError("update release id collides with different staged content")
            shutil.rmtree(target)
        try:
            payload = temporary / "payload"
            payload.mkdir()
            self._extract(archive, manifest, payload)
            for name in _RUNTIME_SUPPORT_FILES:
                source = self.bundle_root / name
                if not source.is_file() or source.is_symlink():
                    raise ValueError(f"installed runtime support file is missing: {name}")
                shutil.copy2(source, payload / name)
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            (temporary / "signature.json").write_bytes(archive.read("signature.json"))
            os.replace(temporary, target)
            target_record = {
                "schema_version": 1,
                "kind": "payload",
                "release_id": _release_id(release),
                "release": _release_dict(release),
            }
            _atomic_write(
                self.pending_path,
                {
                    "schema_version": 1,
                    "previous": previous_record,
                    "target": target_record,
                },
            )
            _atomic_write(self.current_path, target_record)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            try:
                current_unchanged = self._current_record() == previous_record
            except Exception:
                current_unchanged = False
            if current_unchanged and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError:
                    pass
            if current_unchanged and self.pending_path.exists() and not target.exists():
                self.pending_path.unlink()
            raise
        finally:
            archive.close()
        return UpdateReceipt(release=release, previous=previous)

    def complete(self, *, success: bool) -> RuntimeSelection:
        """Commit a healthy activation or automatically restore its predecessor."""

        if success:
            with _file_lock(self.transaction_lock_path):
                return self._complete_unlocked(success=True)
        with _file_lock(self.runtime_lock_path):
            with _file_lock(self.transaction_lock_path):
                return self._complete_unlocked(success=False)

    def _complete_unlocked(self, *, success: bool) -> RuntimeSelection:
        if not self.pending_path.exists():
            raise ValueError("no update is pending health verification")
        pending = _read_json_bytes(self.pending_path.read_bytes(), name="pending update")
        if (
            set(pending) != {"schema_version", "previous", "target"}
            or pending.get("schema_version") != 1
        ):
            raise ValueError("invalid pending update")
        previous = pending["previous"]
        target = pending["target"]
        if not isinstance(previous, dict) or not isinstance(target, dict):
            raise ValueError("invalid pending update pointers")
        self._selection(previous)
        self._selection(target)
        current = self._current_record()
        if current != target:
            if not success and current == previous:
                self.pending_path.unlink()
                return self._selection(previous)
            raise ValueError("active payload does not match pending update")
        if success:
            _atomic_write(
                self.rollback_path,
                {
                    "schema_version": 1,
                    "from": target,
                    "to": previous,
                },
            )
        else:
            _atomic_write(self.current_path, previous)
        self.pending_path.unlink()
        return self._resolve_active_unlocked()

    def rollback(self) -> RuntimeSelection:
        """Consume the single retained rollback pointer after a committed update."""

        with _file_lock(self.runtime_lock_path):
            with _file_lock(self.transaction_lock_path):
                return self._rollback_unlocked()

    def _rollback_unlocked(self) -> RuntimeSelection:
        if self.pending_path.exists():
            raise ValueError("pending health verification must be completed before rollback")
        if not self.rollback_path.exists():
            raise ValueError("no committed update is available for rollback")
        rollback = _read_json_bytes(self.rollback_path.read_bytes(), name="update rollback")
        if set(rollback) != {"schema_version", "from", "to"} or rollback.get("schema_version") != 1:
            raise ValueError("invalid update rollback state")
        source = rollback["from"]
        target = rollback["to"]
        if not isinstance(source, dict) or not isinstance(target, dict):
            raise ValueError("invalid update rollback pointers")
        self._selection(source)
        self._selection(target)
        if self._current_record() != source:
            raise ValueError("active payload does not match rollback source")
        _atomic_write(self.current_path, target)
        self.rollback_path.unlink()
        return self._resolve_active_unlocked()


__all__ = ["PayloadUpdater", "ReleaseIdentity", "RuntimeSelection", "UpdateReceipt"]
