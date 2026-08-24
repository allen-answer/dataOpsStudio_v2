from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

# R3 test seam: generate a synthetic release signature; no key bytes are persisted.
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: TID251
    Ed25519PrivateKey,
)

import updater.core as updater_core
from tools.package.build_identity import BuildIdentity
from tools.package.build_payload_update import finalize_payload_update, prepare_payload_update
from tools.package.update_compatibility import build_update_compatibility
from updater import PayloadUpdater


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_compatibility_sources(repo: Path) -> None:
    (repo / "desktop/src-tauri").mkdir(parents=True)
    (repo / "bundle-scripts").mkdir()
    (repo / "updater").mkdir()
    (repo / "tools/package").mkdir(parents=True)
    (repo / "app/infrastructure/license").mkdir(parents=True)
    (repo / "app/db/migrations").mkdir(parents=True)
    (repo / "desktop/src-tauri/Cargo.lock").write_text("shell-v1\n", encoding="utf-8")
    (repo / "bundle-scripts/start.sh").write_text("start-v1\n", encoding="utf-8")
    (repo / "updater/__init__.py").write_text("updater-v1\n", encoding="utf-8")
    (repo / "tools/package/runtime-pins.py").write_text("postgres-v1\n", encoding="utf-8")
    (repo / "app/infrastructure/license/signatures.py").write_text("verify-v1\n", encoding="utf-8")
    (repo / "app/db/migrations/env.py").write_text("migration-v1\n", encoding="utf-8")
    (repo / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (repo / ".python-version").write_text("3.12\n", encoding="ascii")
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "dataops-studio"\n'
        'version = "2.0.1"\n'
        'requires-python = ">=3.12,<3.13"\n'
        'dependencies = ["example>=1"]\n'
        "[project.optional-dependencies]\n"
        'oracle = ["oracledb>=2"]\n',
        encoding="utf-8",
    )


def _compatibility(repo: Path, requirements: Path) -> dict[str, object]:
    return build_update_compatibility(
        repo_root=repo,
        platform="win10-x64",
        requirements=requirements,
        python={"version": "3.12.13", "sha256": "a" * 64},
        uv={"version": "0.11.23", "sha256": "b" * 64},
        postgres={"version": "16.14-1", "sha256": "c" * 64},
    )


def _signed_update(
    tmp_path: Path,
    *,
    version: str = "2.0.2",
    commit: str = "d" * 40,
) -> tuple[PayloadUpdater, Path, Path, Path, dict[str, object]]:
    repo = tmp_path / "repo"
    _write_compatibility_sources(repo)
    (repo / "app/__init__.py").write_text("", encoding="utf-8")
    (repo / "app/feature.py").write_text("VALUE = 'updated'\n", encoding="utf-8")
    (repo / "app/launcher.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "marker = os.environ.get('TEST_RUN_MARKER')\n"
        "if marker:\n"
        "    Path(marker).write_text(os.environ['DATAOPS_BUILD_COMMIT'], encoding='ascii')\n",
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend-dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>updated</h1>\n", encoding="utf-8")
    requirements = tmp_path / "requirements-frozen.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    compatibility = _compatibility(repo, requirements)

    bundle = tmp_path / "bundle"
    (bundle / "runtime").mkdir(parents=True)
    for name in ("alembic.ini", "pyproject.toml", "uv.lock", ".python-version"):
        (bundle / name).write_text(f"base {name}\n", encoding="utf-8")
    (bundle / "runtime/update-compatibility.json").write_text(
        json.dumps(compatibility), encoding="utf-8"
    )
    work = tmp_path / "update-work"
    manifest_path = prepare_payload_update(
        repo_root=repo,
        frontend_dist=frontend,
        requirements=requirements,
        base_compatibility=compatibility,
        identity=BuildIdentity(
            version=version,
            commit=commit,
            image_version=f"{version}-win10-x64-payload",
        ),
        work_dir=work,
    )

    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(manifest_path.read_bytes())
    signature_path = tmp_path / "manifest.sig"
    signature_path.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")
    package = finalize_payload_update(
        work_dir=work,
        key_id="test-release",
        signature_file=signature_path,
        output=tmp_path / "update.dupdate",
    )
    public_bytes = private_key.public_key().public_bytes_raw()
    trust_store = tmp_path / "trusted-update-keys.json"
    trust_store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": [
                    {
                        "key_id": "test-release",
                        "algorithm": "ed25519",
                        "public_key": base64.b64encode(public_bytes).decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    updater = PayloadUpdater(bundle_root=bundle, state_root=tmp_path / "state")
    return updater, package, trust_store, bundle, compatibility


def _add_signed_payload_path(
    package: Path,
    trust_store: Path,
    *,
    path: str,
    content: bytes,
) -> None:
    with zipfile.ZipFile(package) as source:
        manifest = json.loads(source.read("manifest.json"))
        payload_entries = {
            item.filename: source.read(item.filename)
            for item in source.infolist()
            if item.filename.startswith("payload/") and not item.is_dir()
        }
    manifest["files"].append(
        {
            "path": path,
            "sha256": sha256(content).hexdigest(),
            "size": len(content),
        }
    )
    manifest_bytes = _canonical_json(manifest)
    private_key = Ed25519PrivateKey.generate()
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "key_id": "test-release",
        "signature": base64.b64encode(private_key.sign(manifest_bytes)).decode("ascii"),
    }
    replacement = package.with_suffix(".replacement")
    with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("manifest.json", manifest_bytes)
        target.writestr("signature.json", _canonical_json(signature))
        for name, data in payload_entries.items():
            target.writestr(name, data)
        target.writestr(f"payload/{path}", content)
    os.replace(replacement, package)
    public_bytes = private_key.public_key().public_bytes_raw()
    trust_store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": [
                    {
                        "key_id": "test-release",
                        "algorithm": "ed25519",
                        "public_key": base64.b64encode(public_bytes).decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_update_compatibility_binds_runtime_and_non_payload_sources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_compatibility_sources(repo)
    requirements = tmp_path / "requirements-frozen.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")

    compatibility = _compatibility(repo, requirements)

    assert compatibility["schema_version"] == 1
    assert compatibility["platform"] == "win10-x64"
    assert compatibility["requirements_sha256"] != ""
    assert compatibility["compatibility_id"] != ""
    assert set(cast(dict[str, str], compatibility["source_fingerprints"])) == {
        "desktop",
        "migrations",
        "packaging",
        "runtime_project",
        "runtime_scripts",
        "updater",
    }

    (repo / "desktop/src-tauri/target/debug").mkdir(parents=True)
    (repo / "desktop/src-tauri/target/debug/transient.exe").write_bytes(b"generated")
    assert _compatibility(repo, requirements) == compatibility

    (repo / "desktop/src-tauri/Cargo.lock").write_text("shell-v2\n", encoding="utf-8")
    changed = _compatibility(repo, requirements)
    assert changed["compatibility_id"] != compatibility["compatibility_id"]


def test_signed_payload_installs_and_failed_health_completion_rolls_back(
    tmp_path: Path,
) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    receipt = updater.install(package=package, trust_store=trust_store)
    selected = updater.resolve_active()

    assert receipt.release.commit == "d" * 40
    assert selected.root.joinpath("app/feature.py").read_text(encoding="utf-8") == (
        "VALUE = 'updated'\n"
    )
    assert selected.root.joinpath("frontend/dist/index.html").read_text(encoding="utf-8") == (
        "<h1>updated</h1>\n"
    )
    assert selected.release == receipt.release

    updater.complete(success=False)

    rolled_back = updater.resolve_active()
    assert rolled_back.root == bundle.resolve()
    assert rolled_back.release is None


def test_bad_signature_and_unmanifested_path_never_switch_active_payload(tmp_path: Path) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    wrong_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    trust = json.loads(trust_store.read_text(encoding="utf-8"))
    trust["keys"][0]["public_key"] = base64.b64encode(wrong_key).decode("ascii")
    trust_store.write_text(json.dumps(trust), encoding="utf-8")

    with pytest.raises(ValueError, match="signature"):
        updater.install(package=package, trust_store=trust_store)

    assert updater.resolve_active().root == bundle.resolve()
    assert not updater.versions_root.exists()

    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path / "extra")
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("payload/../outside.txt", b"escape")

    with pytest.raises(ValueError, match="unmanifested"):
        updater.install(package=package, trust_store=trust_store)

    assert updater.resolve_active().root == bundle.resolve()
    assert not (tmp_path / "extra/outside.txt").exists()


def test_signed_payload_rejects_windows_drive_path_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    _add_signed_payload_path(
        package,
        trust_store,
        path="app/Z:/escape.txt",
        content=b"must stay in staging",
    )
    original_mkdir = Path.mkdir

    def guarded_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.drive.casefold() == "z:":
            raise AssertionError("payload escaped the staging filesystem boundary")
        original_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)

    with pytest.raises(ValueError, match="unsafe update path"):
        updater.install(package=package, trust_store=trust_store)

    assert updater.resolve_active().root == bundle.resolve()


def test_successful_health_completion_keeps_one_manual_rollback(tmp_path: Path) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    receipt = updater.install(package=package, trust_store=trust_store)

    committed = updater.complete(success=True)

    assert committed.release == receipt.release
    assert not updater.pending_path.exists()
    assert updater.rollback_path.exists()

    restored = updater.rollback()

    assert restored.root == bundle.resolve()
    assert restored.release is None
    assert not updater.rollback_path.exists()


@pytest.mark.parametrize("failed_pointer", ["pending", "current"])
def test_interrupted_pointer_write_leaves_release_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_pointer: str,
) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    original_atomic_write = updater_core._atomic_write
    failed_path = updater.pending_path if failed_pointer == "pending" else updater.current_path

    def fail_once(path: Path, value: object) -> None:
        if path == failed_path:
            raise OSError(f"simulated {failed_pointer} pointer write failure")
        original_atomic_write(path, value)

    monkeypatch.setattr(updater_core, "_atomic_write", fail_once)

    with pytest.raises(OSError, match=f"simulated {failed_pointer}"):
        updater.install(package=package, trust_store=trust_store)

    assert updater.resolve_active().root == bundle.resolve()
    assert not updater.pending_path.exists()
    assert not list(updater.versions_root.glob("*"))

    monkeypatch.setattr(updater_core, "_atomic_write", original_atomic_write)
    receipt = updater.install(package=package, trust_store=trust_store)
    assert receipt.release.commit == "d" * 40


@pytest.mark.parametrize("pending_survived", [False, True])
def test_restart_recovers_unreferenced_signed_target_after_power_loss(
    tmp_path: Path,
    pending_survived: bool,
) -> None:
    updater, package, trust_store, _bundle, _compatibility_value = _signed_update(tmp_path)
    updater.install(package=package, trust_store=trust_store)
    stranded_targets = list(updater.versions_root.glob("*"))
    assert len(stranded_targets) == 1
    updater.current_path.unlink()
    if not pending_survived:
        updater.pending_path.unlink()

    receipt = updater.install(package=package, trust_store=trust_store)

    assert receipt.release.commit == "d" * 40
    assert updater.resolve_active().release == receipt.release


def test_pending_recovery_preserves_stranded_release_for_different_signed_package(
    tmp_path: Path,
) -> None:
    updater, package_a, trust_a, _bundle, _compatibility_value = _signed_update(tmp_path / "a")
    updater.install(package=package_a, trust_store=trust_a)
    updater.current_path.unlink()
    pending_before = updater.pending_path.read_bytes()
    targets_before = {path.name for path in updater.versions_root.iterdir()}
    _other_updater, package_b, trust_b, _other_bundle, _other_compatibility = _signed_update(
        tmp_path / "b",
        version="2.0.3",
        commit="e" * 40,
    )

    with pytest.raises(ValueError, match="different signed release"):
        updater.install(package=package_b, trust_store=trust_b)

    assert updater.pending_path.read_bytes() == pending_before
    assert {path.name for path in updater.versions_root.iterdir()} == targets_before


def test_launcher_environment_uses_selected_payload_identity_and_paths(tmp_path: Path) -> None:
    updater, package, trust_store, _bundle, _compatibility_value = _signed_update(tmp_path)
    receipt = updater.install(package=package, trust_store=trust_store)

    environment = updater.launcher_environment(
        {"PATH": "existing-path", "PYTHONPATH": "existing-pythonpath"}
    )
    selected = updater.resolve_active()

    assert environment["PATH"] == "existing-path"
    assert environment["DATAOPS_BUILD_VERSION"] == receipt.release.version
    assert environment["DATAOPS_BUILD_COMMIT"] == receipt.release.commit
    assert environment["DATAOPS_IMAGE_VERSION"] == receipt.release.image_version
    assert environment["DATAOPS_FRONTEND_DIST"] == str(selected.root / "frontend/dist")
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(selected.root)


def test_launcher_runner_executes_the_selected_signed_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, package, trust_store, _bundle, _compatibility_value = _signed_update(tmp_path)
    receipt = updater.install(package=package, trust_store=trust_store)
    marker = tmp_path / "selected-app-ran.txt"
    monkeypatch.setenv("TEST_RUN_MARKER", str(marker))

    return_code = updater.run_launcher(
        python=Path(sys.executable),
        home=tmp_path / "home",
        pg_bin_dir=tmp_path / "pgsql/bin",
        uv=tmp_path / "uv",
        arguments=["status"],
    )

    assert return_code == 0
    assert marker.read_text(encoding="ascii") == receipt.release.commit


def test_running_launcher_holds_update_transaction_and_one_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, package, trust_store, bundle, _compatibility_value = _signed_update(tmp_path)
    observed: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        observed.update(command=command, cwd=cwd, env=env, check=check)
        with pytest.raises(ValueError, match="another update transaction"):
            updater.install(package=package, trust_store=trust_store)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("updater.core.subprocess.run", fake_run)

    assert (
        updater.run_launcher(
            python=Path(sys.executable),
            home=tmp_path / "home",
            pg_bin_dir=tmp_path / "pgsql/bin",
            uv=tmp_path / "uv",
            arguments=["up"],
        )
        == 0
    )

    assert observed["cwd"] == bundle.resolve()
    environment = cast(dict[str, str], observed["env"])
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(observed["cwd"])


def test_healthy_completion_is_allowed_while_selected_launcher_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, package, trust_store, _bundle, _compatibility_value = _signed_update(tmp_path)
    receipt = updater.install(package=package, trust_store=trust_store)

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, check
        completed = updater.complete(success=True)
        assert completed.release == receipt.release
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("updater.core.subprocess.run", fake_run)

    assert (
        updater.run_launcher(
            python=Path(sys.executable),
            home=tmp_path / "home",
            pg_bin_dir=tmp_path / "pgsql/bin",
            uv=tmp_path / "uv",
            arguments=["up"],
        )
        == 0
    )
    assert not updater.pending_path.exists()
    assert updater.rollback_path.exists()


def test_stop_runner_can_join_active_runtime_without_unlocking_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, package, trust_store, _bundle, _compatibility_value = _signed_update(tmp_path)
    commands: list[str] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, check
        verb = command[-1]
        commands.append(verb)
        if verb == "up":
            assert (
                updater.run_launcher(
                    python=Path(sys.executable),
                    home=tmp_path / "home",
                    pg_bin_dir=tmp_path / "pgsql/bin",
                    uv=tmp_path / "uv",
                    arguments=["stop"],
                )
                == 0
            )
            with pytest.raises(ValueError, match="another update transaction"):
                updater.install(package=package, trust_store=trust_store)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("updater.core.subprocess.run", fake_run)

    assert (
        updater.run_launcher(
            python=Path(sys.executable),
            home=tmp_path / "home",
            pg_bin_dir=tmp_path / "pgsql/bin",
            uv=tmp_path / "uv",
            arguments=["up"],
        )
        == 0
    )
    assert commands == ["up", "stop"]


@pytest.mark.parametrize(
    ("changed_path", "replacement"),
    [
        ("desktop/src-tauri/Cargo.lock", "shell-v2\n"),
        ("tools/package/runtime-pins.py", "postgres-v2\n"),
        (".python-version", "3.13\n"),
        ("app/db/migrations/env.py", "migration-v2\n"),
        ("updater/__init__.py", "updater-v2\n"),
    ],
)
def test_payload_build_rejects_tauri_postgres_migration_or_updater_changes(
    tmp_path: Path,
    changed_path: str,
    replacement: str,
) -> None:
    repo = tmp_path / "repo"
    _write_compatibility_sources(repo)
    (repo / "app/__init__.py").write_text("", encoding="utf-8")
    frontend = tmp_path / "frontend-dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("updated\n", encoding="utf-8")
    requirements = tmp_path / "requirements-frozen.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    compatibility = _compatibility(repo, requirements)
    (repo / changed_path).write_text(replacement, encoding="utf-8")

    with pytest.raises(ValueError, match=r"runtime.*changed"):
        prepare_payload_update(
            repo_root=repo,
            frontend_dist=frontend,
            requirements=requirements,
            base_compatibility=compatibility,
            identity=BuildIdentity(
                version="2.0.2",
                commit="d" * 40,
                image_version="2.0.2-win10-x64-payload",
            ),
            work_dir=tmp_path / "work",
        )


def test_payload_build_rejects_wheel_set_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_compatibility_sources(repo)
    (repo / "app/__init__.py").write_text("", encoding="utf-8")
    frontend = tmp_path / "frontend-dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("updated\n", encoding="utf-8")
    requirements = tmp_path / "requirements-frozen.txt"
    requirements.write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    compatibility = _compatibility(repo, requirements)
    requirements.write_text("example==2 --hash=sha256:def\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"runtime.*changed"):
        prepare_payload_update(
            repo_root=repo,
            frontend_dist=frontend,
            requirements=requirements,
            base_compatibility=compatibility,
            identity=BuildIdentity(
                version="2.0.2",
                commit="d" * 40,
                image_version="2.0.2-win10-x64-payload",
            ),
            work_dir=tmp_path / "work",
        )


def test_installed_postgres_compatibility_mismatch_requires_full_package(
    tmp_path: Path,
) -> None:
    updater, package, trust_store, bundle, compatibility = _signed_update(tmp_path)
    postgres = cast(dict[str, str], compatibility["postgres"])
    postgres["sha256"] = "e" * 64
    (bundle / "runtime/update-compatibility.json").write_text(
        json.dumps(compatibility), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="requires a full package"):
        updater.install(package=package, trust_store=trust_store)

    assert updater.resolve_active().root == bundle.resolve()
    assert not updater.versions_root.exists()
