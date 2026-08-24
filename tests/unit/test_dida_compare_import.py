"""Offline DIDA compare manifest generator behavior."""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

from app.domain.workflow import WorkflowSpec
from tools import dida_compare_import


class _RecordingApi:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> Any:
        self.calls.append((method, path, body))
        return self.responses.pop(0)


def _inference_response() -> dict[str, object]:
    return {
        "columns": [
            {"name": "AMOUNT", "type": "decimal"},
            {"name": "INDICES_OPT", "type": "datetime"},
        ],
        "mappings": [
            {
                "source_column": "BELT_ORG",
                "target_column": "BELT_ORG",
                "source_type": "string",
            },
            {
                "source_column": "INDICES_DT",
                "target_column": "INDICES_DT",
                "source_type": "string",
            },
            {
                "source_column": "AMOUNT",
                "target_column": "AMOUNT",
                "source_type": "decimal",
            },
            {
                "source_column": "INDICES_OPT",
                "target_column": "INDICES_OPT",
                "source_type": "datetime",
            },
        ],
        "compare_rules": {
            "key_columns": [],
            "ignore_columns": [],
            "column_mappings": {
                "BELT_ORG": "BELT_ORG",
                "INDICES_DT": "INDICES_DT",
                "AMOUNT": "AMOUNT",
                "INDICES_OPT": "INDICES_OPT",
            },
            "numeric_scale": 6,
        },
    }


def _expected_task_columns() -> list[dict[str, object]]:
    return [
        {"name": "BELT_ORG", "type": "string", "primary_key": True},
        {"name": "INDICES_DT", "type": "string", "primary_key": True},
        {"name": "AMOUNT", "type": "decimal"},
        {"name": "INDICES_OPT", "type": "datetime"},
    ]


def _inference_with_different_auto_key() -> dict[str, object]:
    inference = _inference_response()
    columns = _expected_task_columns()
    for column in columns:
        column.pop("primary_key", None)
    inference["columns"] = columns
    mappings = list(cast(list[dict[str, object]], inference["mappings"]))
    mappings.append(
        {
            "source_column": "AUTO_ID",
            "target_column": "AUTO_ID",
            "source_type": "integer",
        }
    )
    inference["mappings"] = mappings
    rules = dict(cast(dict[str, object], inference["compare_rules"]))
    rules["key_columns"] = ["AUTO_ID"]
    column_mappings = dict(cast(dict[str, str], rules["column_mappings"]))
    column_mappings["AUTO_ID"] = "AUTO_ID"
    rules["column_mappings"] = column_mappings
    inference["compare_rules"] = rules
    return inference


def _created_task_response() -> dict[str, object]:
    return {
        "id": "task-1",
        "source_id": "source-ds",
        "target_id": "target-ds",
        "source_ref": {
            "kind": "sql",
            "sql": "select * from MART.DWS_SAMPLE_MONTHLY where period_code = ${period}",
        },
        "target_ref": {
            "kind": "sql",
            "sql": "select * from MART.DWS_SAMPLE_MONTHLY where period_code = ${period}",
        },
        "columns": _expected_task_columns(),
        "compare_rules": {
            "key_columns": ["BELT_ORG", "INDICES_DT"],
            "ignore_columns": ["INDICES_OPT"],
            "column_mappings": {
                "BELT_ORG": "BELT_ORG",
                "INDICES_DT": "INDICES_DT",
                "AMOUNT": "AMOUNT",
                "INDICES_OPT": "INDICES_OPT",
            },
            "numeric_scale": 6,
            "empty_as_null": True,
            "trim_strings": True,
            "schema_policy": "warn",
        },
        "run_limits": {"max_rows": 250, "query_timeout_seconds": 1800},
    }


def _apply_table(
    table: str = "MART.DWS_SAMPLE_MONTHLY",
    *,
    status: str = "ok",
) -> dict[str, object]:
    return {
        "table": table,
        "layer": "dws",
        "period_predicate": "period_code = ${period}",
        "key": ["belt_org", "indices_dt"],
        "status": status,
    }


def _apply_manifest_fixture(*tables: dict[str, object]) -> dict[str, object]:
    return {
        "flow_id": "flow-alpha",
        "tables": list(tables) or [_apply_table()],
        "skipped": [],
    }


def _notification_spec() -> dict[str, object]:
    return {
        "nodes": [{"id": "old", "job_kind": "sleep", "payload": {"duration_seconds": 1}}],
        "edges": [],
        "notifications": [
            {
                "id": "ops",
                "channel": "webhook",
                "url_secret_ref": "ref-synthetic",
                "events": ["all"],
                "enabled": True,
            }
        ],
    }


def test_apply_creates_runnable_task_and_parallel_workflow() -> None:
    api = _RecordingApi(
        [
            [],
            [],
            _inference_response(),
            _created_task_response(),
            {"id": "workflow-1"},
        ]
    )
    manifest = _apply_manifest_fixture(
        _apply_table(),
        _apply_table("MART.DWS_REVIEW_ONLY", status="needs_review"),
    )

    report = dida_compare_import.apply_manifest(
        manifest,
        project_id="project-1",
        source_id="source-ds",
        target_id="target-ds",
        prefix="monthly",
        notify_target=None,
        max_rows=250,
        api=api,
    )

    assert report.tasks_created == 1
    assert report.tasks_updated == 0
    assert report.needs_review_skipped == 1
    assert report.workflow_created is True
    infer_body = api.calls[2][2]
    assert infer_body == {
        "source_id": "source-ds",
        "target_id": "target-ds",
        "source_table": {"schema_name": "MART", "table_name": "DWS_SAMPLE_MONTHLY"},
        "target_table": {"schema_name": "MART", "table_name": "DWS_SAMPLE_MONTHLY"},
    }
    task_body = api.calls[3][2]
    assert task_body is not None
    assert task_body["name"] == "monthly|MART.DWS_SAMPLE_MONTHLY"
    assert task_body["source_ref"] == {
        "kind": "sql",
        "sql": "select * from MART.DWS_SAMPLE_MONTHLY where period_code = ${period}",
    }
    assert task_body["columns"] == _expected_task_columns()
    assert task_body["compare_rules"] == _created_task_response()["compare_rules"]
    assert task_body["run_limits"] == {"max_rows": 250}
    workflow_body = api.calls[4][2]
    assert workflow_body is not None
    assert workflow_body["name"] == "monthly|flow-alpha"
    spec = workflow_body["spec"]
    assert isinstance(spec, dict)
    assert spec["edges"] == []
    assert spec["nodes"] == [
        {
            "id": "c0",
            "job_kind": "compare_run",
            "payload": {
                "task_id": "task-1",
                "source_id": "source-ds",
                "target_id": "target-ds",
                "source_ref": _created_task_response()["source_ref"],
                "target_ref": _created_task_response()["target_ref"],
                "columns": _created_task_response()["columns"],
                "compare_rules": _created_task_response()["compare_rules"],
                "run_limits": _created_task_response()["run_limits"],
            },
            "timeout_seconds": 1800,
        }
    ]


def test_apply_updates_existing_task_and_workflow_without_duplicates() -> None:
    existing_spec = {
        "nodes": [{"id": "old", "job_kind": "sleep", "payload": {"duration_seconds": 1}}],
        "edges": [],
        "schedule": {"cron": "0 3 * * *", "enabled": True},
        "variables": {"period": "202601"},
        "notifications": [
            {
                "id": "ops",
                "channel": "webhook",
                "url_secret_ref": "ref-synthetic",
                "events": ["all"],
                "enabled": True,
            }
        ],
    }
    api = _RecordingApi(
        [
            [{"id": "task-1", "name": "monthly|MART.DWS_SAMPLE_MONTHLY"}],
            [{"id": "workflow-1", "name": "monthly|flow-alpha", "enabled": False}],
            _inference_response(),
            _created_task_response(),
            {"id": "workflow-1", "name": "monthly|flow-alpha", "spec": existing_spec},
            {"id": "workflow-1"},
        ]
    )
    manifest = _apply_manifest_fixture()

    report = dida_compare_import.apply_manifest(
        manifest,
        project_id="project-1",
        source_id="source-ds",
        target_id="target-ds",
        prefix="monthly",
        notify_target=None,
        max_rows=250,
        api=api,
    )

    assert report.tasks_created == 0
    assert report.tasks_updated == 1
    assert report.workflow_created is False
    assert report.workflow_updated is True
    assert [method for method, _path, _body in api.calls] == [
        "GET",
        "GET",
        "POST",
        "PATCH",
        "GET",
        "PUT",
    ]
    task_patch = api.calls[3][2]
    assert task_patch is not None
    assert "project_id" not in task_patch
    assert task_patch["name"] == "monthly|MART.DWS_SAMPLE_MONTHLY"
    workflow_put = api.calls[5][2]
    assert workflow_put is not None
    assert "enabled" not in workflow_put
    updated_spec = workflow_put["spec"]
    assert isinstance(updated_spec, dict)
    assert updated_spec["schedule"] == existing_spec["schedule"]
    assert "variables" not in updated_spec
    assert "notifications" not in updated_spec
    assert updated_spec["nodes"][0]["id"] == "c0"


def test_apply_validates_all_inference_results_before_writing_tasks() -> None:
    invalid_inference = _inference_response()
    invalid_inference["columns"] = [{"name": "OTHER_KEY", "type": "string"}]
    invalid_inference["mappings"] = []
    api = _RecordingApi([[], [], _inference_response(), invalid_inference])
    manifest = _apply_manifest_fixture(
        _apply_table("MART.DWS_SAMPLE_ONE"),
        _apply_table("MART.DWS_SAMPLE_TWO"),
    )

    with pytest.raises(ValueError, match="missing inferred key column"):
        dida_compare_import.apply_manifest(
            manifest,
            project_id="project-1",
            source_id="source-ds",
            target_id="target-ds",
            prefix="monthly",
            notify_target=None,
            max_rows=250,
            api=api,
        )

    assert [method for method, _path, _body in api.calls] == ["GET", "GET", "POST", "POST"]


def test_apply_keeps_inferred_auto_key_when_manifest_selects_different_keys() -> None:
    task_response = _created_task_response()
    task_response["columns"] = [
        *_expected_task_columns(),
        {"name": "AUTO_ID", "type": "integer"},
    ]
    api = _RecordingApi(
        [[], [], _inference_with_different_auto_key(), task_response, {"id": "workflow-1"}]
    )
    manifest = _apply_manifest_fixture()

    dida_compare_import.apply_manifest(
        manifest,
        project_id="project-1",
        source_id="source-ds",
        target_id="target-ds",
        prefix="monthly",
        notify_target=None,
        max_rows=250,
        api=api,
    )

    task_body = api.calls[3][2]
    assert task_body is not None
    columns = task_body["columns"]
    assert isinstance(columns, list)
    assert [column["name"] for column in columns] == [
        "BELT_ORG",
        "INDICES_DT",
        "AMOUNT",
        "INDICES_OPT",
        "AUTO_ID",
    ]


def test_apply_groups_diff_conditions_into_branch_notify_routes() -> None:
    existing_spec = _notification_spec()
    api = _RecordingApi(
        [
            [],
            [{"id": "workflow-1", "name": "monthly|flow-alpha"}],
            {"id": "workflow-1", "name": "monthly|flow-alpha", "spec": existing_spec},
            _inference_response(),
            _created_task_response(),
            {"id": "workflow-1"},
        ]
    )
    manifest = _apply_manifest_fixture()

    report = dida_compare_import.apply_manifest(
        manifest,
        project_id="project-1",
        source_id="source-ds",
        target_id="target-ds",
        prefix="monthly",
        notify_target="ops",
        max_rows=250,
        api=api,
    )

    assert report.workflow_updated is True
    workflow_put = api.calls[-1][2]
    assert workflow_put is not None
    spec = workflow_put["spec"]
    assert isinstance(spec, dict)
    assert "notifications" not in spec
    assert [node["job_kind"] for node in spec["nodes"]] == [
        "compare_run",
        "branch",
        "notify",
        "sleep",
    ]
    assert spec["edges"] == [
        {"source": "c0", "target": "branch"},
        {
            "source": "branch",
            "target": "notify0",
            "when": (
                "${nodes.c0.diff_count}>0||${nodes.c0.only_source_count}>0"
                "||${nodes.c0.only_target_count}>0"
            ),
        },
        {"source": "branch", "target": "no_diff", "is_default": True},
    ]
    assert (
        WorkflowSpec.model_validate({**spec, "notifications": existing_spec["notifications"]})
        .nodes[-1]
        .id
        == "no_diff"
    )


def test_apply_rejects_new_workflow_notify_target_before_task_writes() -> None:
    api = _RecordingApi([[], []])
    manifest = _apply_manifest_fixture()

    with pytest.raises(ValueError, match="existing workflow notification target"):
        dida_compare_import.apply_manifest(
            manifest,
            project_id="project-1",
            source_id="source-ds",
            target_id="target-ds",
            prefix="monthly",
            notify_target="ops",
            max_rows=250,
            api=api,
        )

    assert [method for method, _path, _body in api.calls] == ["GET", "GET"]


def test_apply_rejects_45_table_notify_workflow_before_task_writes() -> None:
    existing_spec = _notification_spec()
    api = _RecordingApi(
        [
            [],
            [{"id": "workflow-1", "name": "monthly|flow-alpha"}],
            {"id": "workflow-1", "name": "monthly|flow-alpha", "spec": existing_spec},
        ]
    )
    manifest = _apply_manifest_fixture(
        *[_apply_table(f"MART.DWS_SAMPLE_{index:02d}") for index in range(45)]
    )

    with pytest.raises(ValueError, match="exceeds MAX_WORKFLOW_NODES"):
        dida_compare_import.apply_manifest(
            manifest,
            project_id="project-1",
            source_id="source-ds",
            target_id="target-ds",
            prefix="monthly",
            notify_target="ops",
            max_rows=250,
            api=api,
        )

    assert api.calls == []


def test_apply_rejects_more_than_50_parallel_tables_before_task_writes() -> None:
    api = _RecordingApi([[], []])
    manifest = _apply_manifest_fixture(
        *[_apply_table(f"MART.DWS_SAMPLE_{index:02d}") for index in range(51)]
    )

    with pytest.raises(ValueError, match="exceeds MAX_WORKFLOW_NODES"):
        dida_compare_import.apply_manifest(
            manifest,
            project_id="project-1",
            source_id="source-ds",
            target_id="target-ds",
            prefix="monthly",
            notify_target=None,
            max_rows=250,
            api=api,
        )

    assert api.calls == []


def test_apply_cli_uses_environment_api_credentials_without_echoing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(_apply_manifest_fixture(), sort_keys=False),
        encoding="utf-8",
    )
    responses = [
        [],
        [],
        _inference_response(),
        _created_task_response(),
        {"id": "workflow-1"},
    ]
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        assert timeout == 30
        requests.append(request)
        return io.BytesIO(json.dumps(responses.pop(0)).encode("utf-8"))

    credential_placeholder = "<DATAOPS_API_TOKEN>"
    monkeypatch.setenv("DATAOPS_API_URL", "https://api.invalid")
    monkeypatch.setenv("DATAOPS_API_TOKEN", credential_placeholder)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert (
        dida_compare_import.main(
            [
                "apply",
                "--manifest",
                str(manifest_path),
                "--project",
                "project-1",
                "--source-ds",
                "source-ds",
                "--target-ds",
                "target-ds",
                "--prefix",
                "monthly",
                "--max-rows",
                "250",
            ]
        )
        == 0
    )

    assert requests[0].full_url.startswith("https://api.invalid/api/compare/tasks?")
    assert all(
        request.get_header("Authorization") == f"Bearer {credential_placeholder}"
        for request in requests
    )
    stdout = capsys.readouterr().out
    assert json.loads(stdout) == {
        "needs_review_skipped": 0,
        "tasks_created": 1,
        "tasks_updated": 0,
        "workflow_created": True,
        "workflow_updated": False,
    }
    assert credential_placeholder not in stdout


def _make_flow_dir(
    export_dir: Path,
    *,
    flow_id: str = "flow-alpha",
    directory_name: str = "synthetic-selected",
) -> Path:
    flow_dir = export_dir / "by_flow" / directory_name
    flow_dir.mkdir(parents=True)
    (flow_dir / "_flow_summary.json").write_text(
        json.dumps({"flow_id": flow_id, "flow_name": "Synthetic flow"}),
        encoding="utf-8",
    )
    return flow_dir


def test_generate_refuses_to_overwrite_existing_manifest(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text("[]", encoding="utf-8")
    output = tmp_path / "manifest.yaml"
    output.write_text("existing-review-decisions\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )

    assert exc_info.value.code == 2
    assert output.read_text(encoding="utf-8") == "existing-review-decisions\n"


def test_generate_emits_known_dws_table_with_verbatim_period_predicate(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    sql_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000001",
                    "task_name": "Synthetic monthly aggregate",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_MONTHLY"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (sql_dir / "synthetic_monthly_10000000000000000001.sql").write_text(
        "delete from MART.DWS_SAMPLE_MONTHLY "
        "where period_code = '${period}' and region_code = \"R1\";\n"
        "insert into MART.DWS_SAMPLE_MONTHLY select 1;\n",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    exit_code = dida_compare_import.main(
        [
            "generate",
            "--export-dir",
            str(export_dir),
            "--flow-id",
            "flow-alpha",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    assert yaml.safe_load(output.read_text(encoding="utf-8")) == {
        "flow_id": "flow-alpha",
        "tables": [
            {
                "table": "MART.DWS_SAMPLE_MONTHLY",
                "layer": "dws",
                "period_predicate": ("period_code = '${period}' and region_code = \"R1\""),
                "key": ["belt_org", "indices_dt"],
                "status": "ok",
            }
        ],
        "skipped": [],
    }


def test_generate_deduplicates_target_written_by_multiple_tasks(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    sql_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000001",
                    "task_name": "Synthetic writer one",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["REPORT.KGRP_SAMPLE_REPORT"],
                },
                {
                    "task_id": "10000000000000000002",
                    "task_name": "Synthetic writer two",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["report.kgrp_sample_report"],
                },
                {
                    "task_id": "10000000000000000003",
                    "task_name": "Other flow writer",
                    "flows": [{"flow_id": "flow-other", "flow_name": "Other flow"}],
                    "target_tables": ["REPORT.KGRP_OTHER_REPORT"],
                },
            ]
        ),
        encoding="utf-8",
    )
    (sql_dir / "synthetic_writer_10000000000000000001.sql").write_text(
        "delete from REPORT.KGRP_SAMPLE_REPORT where report_period = ${period};",
        encoding="utf-8",
    )
    (sql_dir / "synthetic_writer_10000000000000000002.sql").write_text(
        "delete from report.kgrp_sample_report where report_period = ${period};",
        encoding="utf-8",
    )
    (sql_dir / "synthetic_writer_10000000000000000003.sql").write_text(
        "delete from REPORT.KGRP_OTHER_REPORT where report_period = ${period};",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "REPORT.KGRP_SAMPLE_REPORT",
            "layer": "kgrp",
            "period_predicate": "report_period = ${period}",
            "key": ["customer_no", "department_id", "period"],
            "status": "ok",
        }
    ]


def test_generate_records_task_without_sql_in_skipped_section(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000004",
                    "task_name": "Synthetic dependency gate",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["DETAIL.DWD_SAMPLE_DETAIL"],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == []
    assert manifest["skipped"] == [
        {
            "task_id": "10000000000000000004",
            "name": "Synthetic dependency gate",
            "reason": "no_sql_file",
        }
    ]


def test_generate_recovers_no_sql_task_from_matching_flow_dag(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    selected_flow_dir = export_dir / "by_flow" / "synthetic-selected"
    other_flow_dir = export_dir / "by_flow" / "synthetic-other"
    lineage_dir.mkdir(parents=True)
    selected_flow_dir.mkdir(parents=True)
    other_flow_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text("[]", encoding="utf-8")
    (export_dir / "validation.json").write_text(
        json.dumps({"no_sql_tasks": ["10000000000000000009"]}),
        encoding="utf-8",
    )
    (selected_flow_dir / "_flow_summary.json").write_text(
        json.dumps({"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}),
        encoding="utf-8",
    )
    (selected_flow_dir / "_dag.json").write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "shape": "dag-task",
                        "data": {
                            "taskId": "10000000000000000009",
                            "taskName": "Synthetic blocking node",
                        },
                    },
                    {
                        "shape": "dag-task",
                        "data": {
                            "taskId": "10000000000000000009",
                            "taskName": "Synthetic blocking node duplicate",
                        },
                    },
                    {
                        "shape": "dag-task",
                        "data": {
                            "taskId": "10000000000000000010",
                            "taskName": "Synthetic SQL task",
                        },
                    },
                    {"shape": "dag-edge", "data": {}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (other_flow_dir / "_flow_summary.json").write_text(
        json.dumps({"flow_id": "flow-other", "flow_name": "Other flow"}),
        encoding="utf-8",
    )
    (other_flow_dir / "_dag.json").write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "shape": "dag-task",
                        "data": {
                            "taskId": "10000000000000000009",
                            "taskName": "Wrong flow task name",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["skipped"] == [
        {
            "task_id": "10000000000000000009",
            "name": "Synthetic blocking node",
            "reason": "no_sql_file",
        }
    ]


def test_generate_marks_dwd_and_unclassified_tables_for_review(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    sql_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000005",
                    "task_name": "Synthetic detail writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": [
                        "DETAIL.DWD_SAMPLE_DETAIL",
                        "WAREHOUSE.DW_SAMPLE_BASE",
                        "STAGING.SAMPLE_UNCLASSIFIED",
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    (sql_dir / "synthetic_detail_10000000000000000005.sql").write_text(
        "delete from DETAIL.DWD_SAMPLE_DETAIL where business_day = ${day};\n"
        "delete from WAREHOUSE.DW_SAMPLE_BASE where business_day = ${day};\n"
        "delete from STAGING.SAMPLE_UNCLASSIFIED where business_day = '${day}';",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "DETAIL.DWD_SAMPLE_DETAIL",
            "layer": "dwd",
            "period_predicate": "business_day = ${day}",
            "key": [],
            "status": "needs_review",
        },
        {
            "table": "WAREHOUSE.DW_SAMPLE_BASE",
            "layer": "dw",
            "period_predicate": "business_day = ${day}",
            "key": [],
            "status": "needs_review",
        },
        {
            "table": "STAGING.SAMPLE_UNCLASSIFIED",
            "layer": "unknown",
            "period_predicate": "business_day = '${day}'",
            "key": [],
            "status": "needs_review",
        },
    ]


def test_generate_prefers_target_layers_for_schema_named_dw_and_kgrp(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    flow_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000014",
                    "task_name": "Synthetic layered writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": [
                        "KGRP.SAMPLE_REPORT",
                        "WAREHOUSE.SAMPLE_BASE",
                    ],
                    "target_layers": {
                        "kgrp.sample_report": "KGRP",
                        "WAREHOUSE.SAMPLE_BASE": "DW",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (flow_dir / "synthetic_layered_10000000000000000014.sql").write_text(
        "delete from KGRP.SAMPLE_REPORT where period_code = ${period};\n"
        "delete from WAREHOUSE.SAMPLE_BASE where period_code = ${period};",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "KGRP.SAMPLE_REPORT",
            "layer": "kgrp",
            "period_predicate": "period_code = ${period}",
            "key": ["customer_no", "department_id", "period"],
            "status": "ok",
        },
        {
            "table": "WAREHOUSE.SAMPLE_BASE",
            "layer": "dw",
            "period_predicate": "period_code = ${period}",
            "key": [],
            "status": "needs_review",
        },
    ]


def test_generate_requires_extracted_predicate_before_marking_known_table_ok(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    sql_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000006",
                    "task_name": "Synthetic append-only aggregate",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_APPEND_ONLY"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (sql_dir / "synthetic_append_10000000000000000006.sql").write_text(
        "insert into MART.DWS_SAMPLE_APPEND_ONLY select 1;",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "MART.DWS_SAMPLE_APPEND_ONLY",
            "layer": "dws",
            "period_predicate": None,
            "key": ["belt_org", "indices_dt"],
            "status": "needs_review",
        }
    ]


def test_generate_preserves_conflicting_predicates_for_review(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    sql_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000007",
                    "task_name": "Synthetic shared writer one",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_SHARED"],
                },
                {
                    "task_id": "10000000000000000008",
                    "task_name": "Synthetic shared writer two",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_SHARED"],
                },
            ]
        ),
        encoding="utf-8",
    )
    (sql_dir / "synthetic_shared_10000000000000000007.sql").write_text(
        "delete from TEMP.SAMPLE_SCRATCH where stale_flag = 1;\n"
        "delete from MART.DWS_SAMPLE_SHARED s where s.period_code = ${period};",
        encoding="utf-8",
    )
    (sql_dir / "synthetic_shared_10000000000000000008.sql").write_text(
        "delete from MART.DWS_SAMPLE_SHARED where period_code = '${period}';",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "MART.DWS_SAMPLE_SHARED",
            "layer": "dws",
            "period_predicate": None,
            "period_predicate_candidates": [
                {
                    "predicate": "s.period_code = ${period}",
                    "task_id": "10000000000000000007",
                },
                {
                    "predicate": "period_code = '${period}'",
                    "task_id": "10000000000000000008",
                },
            ],
            "key": ["belt_org", "indices_dt"],
            "status": "needs_review",
        }
    ]


def test_generate_preserves_alias_predicate_but_requires_review(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    flow_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000015",
                    "task_name": "Synthetic aliased writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_ALIASED"],
                    "target_layers": {"MART.DWS_SAMPLE_ALIASED": "DWS"},
                }
            ]
        ),
        encoding="utf-8",
    )
    (flow_dir / "synthetic_aliased_10000000000000000015.sql").write_text(
        "delete from MART.DWS_SAMPLE_ALIASED a where a.period_code = ${period} and a.kind = 'S';",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "MART.DWS_SAMPLE_ALIASED",
            "layer": "dws",
            "period_predicate": "a.period_code = ${period} and a.kind = 'S'",
            "key": ["belt_org", "indices_dt"],
            "status": "needs_review",
        }
    ]


def test_generate_reads_all_canonical_sql_files_for_one_task(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    flow_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    task_id = "10000000000000000016"
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": task_id,
                    "task_name": "Synthetic split writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_SPLIT"],
                    "target_layers": {"MART.DWS_SAMPLE_SPLIT": "DWS"},
                }
            ]
        ),
        encoding="utf-8",
    )
    (flow_dir / f"synthetic_part_a_{task_id}.sql").write_text(
        "delete from MART.DWS_SAMPLE_SPLIT where period_code = ${period_a};",
        encoding="utf-8",
    )
    (flow_dir / f"synthetic_part_b_{task_id}.sql").write_text(
        "delete from MART.DWS_SAMPLE_SPLIT where period_code = ${period_b};",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "MART.DWS_SAMPLE_SPLIT",
            "layer": "dws",
            "period_predicate": None,
            "period_predicate_candidates": [
                {"predicate": "period_code = ${period_a}", "task_id": task_id},
                {"predicate": "period_code = ${period_b}", "task_id": task_id},
            ],
            "key": ["belt_org", "indices_dt"],
            "status": "needs_review",
        }
    ]


def test_generate_selects_sql_from_flow_summary_match_not_directory_name(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    selected_dir = _make_flow_dir(export_dir, directory_name="z-selected")
    decoy_dir = _make_flow_dir(
        export_dir,
        flow_id="flow-other",
        directory_name="a-flow-alpha-decoy",
    )
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000011",
                    "task_name": "Synthetic selected writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_SELECTED"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (selected_dir / "synthetic_selected_10000000000000000011.sql").write_text(
        "delete from MART.DWS_SAMPLE_SELECTED where period_code = ${selected_period};",
        encoding="utf-8",
    )
    (decoy_dir / "synthetic_decoy_10000000000000000011.sql").write_text(
        "delete from MART.DWS_SAMPLE_SELECTED where period_code = ${wrong_period};",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"][0]["period_predicate"] == "period_code = ${selected_period}"


def test_generate_does_not_match_sql_by_short_task_id_suffix(tmp_path: Path) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    flow_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "123",
                    "task_name": "Synthetic malformed task id",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_SHORT_ID"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (flow_dir / "synthetic_short_123.sql").write_text(
        "delete from MART.DWS_SAMPLE_SHORT_ID where period_code = ${period};",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == []
    assert manifest["skipped"] == [
        {
            "task_id": "123",
            "name": "Synthetic malformed task id",
            "reason": "no_sql_file",
        }
    ]


def test_generate_marks_table_for_review_when_one_writer_has_no_predicate(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "synthetic-export"
    lineage_dir = export_dir / "lineage"
    flow_dir = _make_flow_dir(export_dir)
    lineage_dir.mkdir(parents=True)
    (lineage_dir / "task_lineage.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "10000000000000000012",
                    "task_name": "Synthetic filtered writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_PARTIAL"],
                },
                {
                    "task_id": "10000000000000000013",
                    "task_name": "Synthetic append writer",
                    "flows": [{"flow_id": "flow-alpha", "flow_name": "Synthetic flow"}],
                    "target_tables": ["MART.DWS_SAMPLE_PARTIAL"],
                },
            ]
        ),
        encoding="utf-8",
    )
    (flow_dir / "synthetic_filtered_10000000000000000012.sql").write_text(
        "delete from MART.DWS_SAMPLE_PARTIAL where period_code = ${period};",
        encoding="utf-8",
    )
    (flow_dir / "synthetic_append_10000000000000000013.sql").write_text(
        "insert into MART.DWS_SAMPLE_PARTIAL select 1;",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.yaml"

    assert (
        dida_compare_import.main(
            [
                "generate",
                "--export-dir",
                str(export_dir),
                "--flow-id",
                "flow-alpha",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    manifest = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert manifest["tables"] == [
        {
            "table": "MART.DWS_SAMPLE_PARTIAL",
            "layer": "dws",
            "period_predicate": "period_code = ${period}",
            "key": ["belt_org", "indices_dt"],
            "status": "needs_review",
        }
    ]
