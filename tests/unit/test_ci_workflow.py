from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
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
