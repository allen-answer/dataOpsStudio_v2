from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_RELEASE_CANDIDATE_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "release-candidate.yml"
_PERMANENT_JOB_MARKERS = {
    "mysql-integration": ("tests/unit/test_mysql_adapter.py",),
    "pg-integration": ("tests/integration/test_postgres_jobbackend.py",),
    "e2e-integration": ("tests/integration/test_e2e_acceptance.py",),
    "cold-start": ("app/launcher.py",),
    "frontend-build": ("frontend/package.json",),
    "agents-doc-sync": ("AGENTS.md", "CLAUDE.md"),
}
_EVIDENCE_STEPS = {
    "mysql-integration": ("pytest -m integration (R6 hard evidence)",),
    "pg-integration": ("pytest tests/integration/ (PG queue concurrency + reaper + ownership)",),
    "e2e-integration": ("pytest e2e acceptance (real PG + real MySQL)",),
    "cold-start": ("§6-§8 cold-start hard evidence (SELECT 1+1 → assert r=2)",),
    "frontend-build": ("vue-tsc + vite build", "Playwright e2e"),
    "agents-doc-sync": ("diff AGENTS.md vs CLAUDE.md (skip first line title)",),
}
_REQUIRED_STEP_CONDITIONS = {
    ("cold-start", "Dump logs on failure"): "failure()",
    ("compose-stack", "Logs on failure"): "failure()",
    ("compose-stack", "Teardown"): "always()",
}


def _workflow_jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    return cast(dict[str, Any], jobs)


def _release_candidate_workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(_RELEASE_CANDIDATE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return cast(dict[str, Any], workflow)


def _release_candidate_jobs() -> dict[str, Any]:
    jobs = _release_candidate_workflow().get("jobs")
    assert isinstance(jobs, dict)
    return cast(dict[str, Any], jobs)


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], job["steps"])


def _step_label(step: dict[str, Any]) -> str:
    return str(step.get("name") or step.get("uses") or "<unnamed step>")


def _runs_when_markers_are_missing(step: dict[str, Any]) -> bool:
    condition = str(step.get("if", ""))
    if not condition:
        return True
    if "hashFiles(" in condition:
        if "!= ''" in condition:
            return False
        if "== ''" in condition:
            return True
    if condition == "always()":
        return True
    if condition == "failure()":
        return False
    raise AssertionError(f"unsupported step condition in missing-marker model: {condition}")


@pytest.mark.parametrize(
    ("job_id", "marker_paths"),
    sorted(_PERMANENT_JOB_MARKERS.items()),
)
def test_permanent_ci_job_cannot_silently_pass_when_marker_is_missing(
    job_id: str,
    marker_paths: tuple[str, ...],
) -> None:
    for marker_path in marker_paths:
        assert (_REPO_ROOT / marker_path).is_file(), f"permanent marker is missing: {marker_path}"

    jobs = _workflow_jobs()
    job = cast(dict[str, Any], jobs[job_id])
    steps = cast(list[dict[str, Any]], job["steps"])
    steps_by_name = {_step_label(step): step for step in steps}
    skipped_evidence = [
        step_name
        for step_name in _EVIDENCE_STEPS[job_id]
        if not _runs_when_markers_are_missing(steps_by_name[step_name])
    ]
    skip_notices = [
        _step_label(step) for step in steps if _step_label(step).startswith("Skip notice")
    ]
    running_skip_notices = [
        _step_label(step)
        for step in steps
        if _step_label(step) in skip_notices and _runs_when_markers_are_missing(step)
    ]

    assert not skipped_evidence and not skip_notices, (
        f"{job_id} can silently pass when a permanent marker is missing; "
        f"skipped evidence={skipped_evidence}, running skip notices={running_skip_notices}"
    )


def test_non_gate_step_conditions_are_preserved() -> None:
    actual_conditions: dict[tuple[str, str], str] = {}
    for job_id, job_value in _workflow_jobs().items():
        job = cast(dict[str, Any], job_value)
        for step in cast(list[dict[str, Any]], job["steps"]):
            if "if" in step:
                actual_conditions[(job_id, _step_label(step))] = str(step["if"])

    assert actual_conditions == _REQUIRED_STEP_CONDITIONS


def test_release_candidate_is_manual_artifact_only_and_read_only() -> None:
    workflow = _release_candidate_workflow()
    # PyYAML 1.1 parses the unquoted YAML key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}

    jobs = _release_candidate_jobs()
    assert set(jobs) == {"preflight", "windows-package", "mint-package"}
    assert jobs["windows-package"]["needs"] == "preflight"
    assert jobs["mint-package"]["needs"] == "preflight"

    rendered = repr(workflow).lower()
    assert "${{ secrets." not in rendered
    assert "update-manifest" not in rendered
    assert "updater" not in rendered
    for job_value in jobs.values():
        job = cast(dict[str, Any], job_value)
        assert "environment" not in job

    allowed_actions = {
        "actions/checkout@v6",
        "actions/setup-node@v6",
        "actions/cache@v4",
        "actions/upload-artifact@v4",
        "astral-sh/setup-uv@v8.2.0",
    }
    used_actions = {
        str(step["uses"])
        for job_value in jobs.values()
        for step in _job_steps(cast(dict[str, Any], job_value))
        if "uses" in step
    }
    assert used_actions <= allowed_actions


def test_release_candidate_preflight_checks_versions_locks_redlines_and_packaging() -> None:
    preflight = cast(dict[str, Any], _release_candidate_jobs()["preflight"])
    assert preflight["runs-on"] == "ubuntu-22.04"
    commands = "\n".join(str(step.get("run", "")) for step in _job_steps(preflight))

    for marker in (
        "uv sync --locked --all-groups",
        "cargo metadata --manifest-path desktop/src-tauri/Cargo.toml --locked",
        "npm ci --ignore-scripts",
        'Path("pyproject.toml")',
        'Path("desktop/src-tauri/Cargo.toml")',
        'Path("desktop/src-tauri/tauri.conf.json")',
        "uv run ruff check app tests tools",
        "uv run ruff format --check app tests tools",
        "uv run sg scan --config tools/lint/sgconfig.yml",
        "gitleaks detect --redact --no-banner --config .gitleaks.toml --exit-code 1",
        "tests/unit/test_redlines.py",
        "tests/unit/test_package_build_identity.py",
        "tests/unit/test_win10_bundle.py",
        "tests/unit/test_mint_bundle.py",
        "tests/unit/test_ci_workflow.py",
    ):
        assert marker in commands


@pytest.mark.parametrize(
    ("job_id", "runner", "builder", "required_flag", "allowed_suffixes"),
    (
        (
            "windows-package",
            "windows-latest",
            "tools/package/build_win10_bundle.py",
            "--gui",
            ("-win10-x64-gui.zip", "-win10-x64-gui.MANIFEST.txt"),
        ),
        (
            "mint-package",
            "ubuntu-22.04",
            "tools/package/build_mint_bundle.py",
            "--cache-dir",
            (
                "-mint21.3-amd64-offline.deb",
                "-mint21.3-amd64-offline.deb.MANIFEST.txt",
            ),
        ),
    ),
)
def test_release_candidate_builds_existing_packages_and_uploads_only_final_artifacts(
    job_id: str,
    runner: str,
    builder: str,
    required_flag: str,
    allowed_suffixes: tuple[str, str],
) -> None:
    job = cast(dict[str, Any], _release_candidate_jobs()[job_id])
    assert job["runs-on"] == runner
    commands = "\n".join(str(step.get("run", "")) for step in _job_steps(job))
    assert builder in commands
    assert required_flag in commands
    assert "release-output" in commands
    assert "dataops-" in commands and "-cache" in commands

    upload = next(
        step for step in _job_steps(job) if step.get("uses") == "actions/upload-artifact@v4"
    )
    upload_config = cast(dict[str, Any], upload["with"])
    paths = tuple(line.strip() for line in str(upload_config["path"]).splitlines())
    assert len(paths) == 2
    assert all("/release-output/dataops-studio-*" in path for path in paths)
    assert all(path.endswith(suffix) for path, suffix in zip(paths, allowed_suffixes, strict=True))
    assert upload_config["if-no-files-found"] == "error"
    assert upload_config["compression-level"] == 0


def test_release_candidate_cache_paths_exclude_outputs_staging_and_secrets() -> None:
    for job_value in _release_candidate_jobs().values():
        job = cast(dict[str, Any], job_value)
        for step in _job_steps(job):
            if step.get("uses") != "actions/cache@v4":
                continue
            cache_path = str(cast(dict[str, Any], step["with"])["path"]).lower()
            assert "release-output" not in cache_path
            assert "dist-" not in cache_path
            assert "desktop/bundle" not in cache_path
            assert "secret" not in cache_path
