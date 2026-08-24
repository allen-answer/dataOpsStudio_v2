"""Offline DIDA compare manifest generator behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from tools import dida_compare_import


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
