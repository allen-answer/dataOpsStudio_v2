"""Build reviewable compare manifests from a parsed DIDA export."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

LAYER_PREFIXES: dict[str, tuple[str, ...]] = {
    "dws": ("DWS_",),
    "kgrp": ("KGRP_",),
    "dwd": ("DWD_",),
    "dw": ("DW_",),
}
EXACT_KNOWN_TABLE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "dws": (
        re.compile(r"^(?:[A-Z0-9_]+\.)?DWS_[A-Z0-9_]+$"),
        re.compile(r"^DWS\.[A-Z0-9_]+$"),
    ),
    "kgrp": (
        re.compile(r"^(?:[A-Z0-9_]+\.)?KGRP_[A-Z0-9_]+$"),
        re.compile(r"^KGRP\.[A-Z0-9_]+$"),
    ),
    "dwd": (),
    "dw": (),
}
KEY_SUGGESTIONS: dict[str, list[str]] = {
    "dws": ["belt_org", "indices_dt"],
    "kgrp": ["customer_no", "department_id", "period"],
    "dwd": [],
    "dw": [],
}
TASK_ID_PATTERN = re.compile(r"\d{20}")
DELETE_WHERE_PATTERN = re.compile(
    r"\bdelete\s+from\s+(?P<table>[^\s;]+)"
    r"(?:\s+(?:as\s+)?(?P<alias>(?!where\b)[A-Z_][A-Z0-9_$#]*))?"
    r"\s+where\s+(?P<predicate>.*?)(?=;|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass
class _TableEntry:
    table: str
    layer: str
    predicate_candidates: list[tuple[str, str]] = field(default_factory=list)
    missing_predicate_task_ids: list[str] = field(default_factory=list)
    has_aliased_predicate: bool = False


def _table_basename(table: str) -> str:
    return table.rsplit(".", maxsplit=1)[-1].strip('"`[]').upper()


def _layer_for_table(table: str) -> str:
    basename = _table_basename(table)
    for layer, prefixes in LAYER_PREFIXES.items():
        if basename.startswith(prefixes):
            return layer
    parts = _normalize_table(table).split(".")
    if len(parts) > 1 and parts[0] in LAYER_PREFIXES:
        return parts[0]
    return "unknown"


def _is_exact_known_pattern(table: str, layer: str) -> bool:
    identifier = _normalize_table(table).upper()
    return any(
        pattern.fullmatch(identifier) for pattern in EXACT_KNOWN_TABLE_PATTERNS.get(layer, ())
    )


def _normalize_table(table: str) -> str:
    return ".".join(part.strip('"`[]').casefold() for part in table.split("."))


def _period_predicates(sql: str, table: str) -> list[tuple[str, bool]]:
    normalized_target = _normalize_table(table)
    predicates: list[tuple[str, bool]] = []
    for match in DELETE_WHERE_PATTERN.finditer(sql):
        if _normalize_table(match.group("table")) == normalized_target:
            predicates.append((match.group("predicate").strip(), match.group("alias") is not None))
    return predicates


def _tasks_from_lineage(lineage_file: Path) -> list[dict[str, Any]]:
    payload = json.loads(lineage_file.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("task_lineage.json must contain a top-level task list")
    return [task for task in payload if isinstance(task, dict)]


def _task_belongs_to_flow(task: dict[str, Any], flow_id: str) -> bool:
    flows = task.get("flows")
    return isinstance(flows, list) and any(
        isinstance(flow, dict) and flow.get("flow_id") == flow_id for flow in flows
    )


def _layer_for_task_target(task: dict[str, Any], table: str) -> str:
    target_layers = task.get("target_layers")
    if isinstance(target_layers, dict):
        normalized_table = _normalize_table(table)
        for mapped_table, mapped_layer in target_layers.items():
            if _normalize_table(str(mapped_table)) != normalized_table:
                continue
            layer = str(mapped_layer).strip().casefold()
            if layer in LAYER_PREFIXES:
                return layer
    return _layer_for_table(table)


def _sql_files_for_task(sql_files: list[Path], task_id: str) -> list[Path]:
    if TASK_ID_PATTERN.fullmatch(task_id) is None:
        return []
    task_suffix = f"_{task_id}"
    return [path for path in sql_files if path.stem.endswith(task_suffix)]


def _skipped_entry(task_id: str, name: str) -> dict[str, str]:
    return {"task_id": task_id, "name": name, "reason": "no_sql_file"}


def _no_sql_task_ids(export_dir: Path) -> set[str]:
    validation_file = export_dir / "validation.json"
    if not validation_file.is_file():
        return set()
    payload = json.loads(validation_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("no_sql_tasks"), list):
        raise ValueError("validation.json must contain a no_sql_tasks list")
    no_sql_tasks = payload["no_sql_tasks"]
    if not all(isinstance(task_id, str) for task_id in no_sql_tasks):
        raise ValueError("validation.json no_sql_tasks must contain only task_id strings")
    return set(no_sql_tasks)


def _matching_flow_dir(export_dir: Path, flow_id: str) -> Path | None:
    for summary_file in sorted((export_dir / "by_flow").glob("*/_flow_summary.json")):
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if isinstance(summary, dict) and summary.get("flow_id") == flow_id:
            return summary_file.parent
    return None


def _validation_skipped_tasks(export_dir: Path, flow_id: str) -> list[dict[str, str]]:
    no_sql_task_ids = _no_sql_task_ids(export_dir)
    if not no_sql_task_ids:
        return []
    flow_dir = _matching_flow_dir(export_dir, flow_id)
    if flow_dir is None:
        raise ValueError(f"no by_flow directory matches flow_id: {flow_id}")
    dag_file = flow_dir / "_dag.json"
    if not dag_file.is_file():
        raise ValueError(f"matching flow directory has no _dag.json: {flow_id}")
    dag = json.loads(dag_file.read_text(encoding="utf-8"))
    cells = dag.get("cells") if isinstance(dag, dict) else None
    if not isinstance(cells, list):
        raise ValueError("_dag.json must contain a cells list")
    skipped_by_id: dict[str, dict[str, str]] = {}
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("shape") != "dag-task":
            continue
        data = cell.get("data")
        if not isinstance(data, dict):
            continue
        task_id = data.get("taskId")
        if not isinstance(task_id, str) or task_id not in no_sql_task_ids:
            continue
        skipped_by_id.setdefault(
            task_id,
            _skipped_entry(task_id, str(data.get("taskName", ""))),
        )
    return list(skipped_by_id.values())


def _generate_manifest(export_dir: Path, flow_id: str) -> dict[str, object]:
    tasks = [
        task
        for task in _tasks_from_lineage(export_dir / "lineage" / "task_lineage.json")
        if _task_belongs_to_flow(task, flow_id)
    ]
    flow_dir = _matching_flow_dir(export_dir, flow_id)
    if flow_dir is None:
        raise ValueError(f"no by_flow directory matches flow_id: {flow_id}")
    sql_files = sorted(flow_dir.rglob("*.sql"))
    table_entries: dict[str, _TableEntry] = {}
    skipped_by_id: dict[str, dict[str, str]] = {}
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        task_sql_files = _sql_files_for_task(sql_files, task_id) if task_id else []
        if not task_sql_files:
            skipped_by_id.setdefault(
                task_id,
                _skipped_entry(task_id, str(task.get("task_name", ""))),
            )
            continue
        sql_texts = [path.read_text(encoding="utf-8") for path in task_sql_files]
        for table_value in task.get("target_tables", []):
            table = str(table_value)
            normalized_table = _normalize_table(table)
            task_layer = _layer_for_task_target(task, table)
            entry = table_entries.setdefault(
                normalized_table,
                _TableEntry(table=table, layer=task_layer),
            )
            if entry.layer == "unknown" and task_layer != "unknown":
                entry.layer = task_layer
            predicates = [
                predicate for sql in sql_texts for predicate in _period_predicates(sql, table)
            ]
            if not predicates and task_id not in entry.missing_predicate_task_ids:
                entry.missing_predicate_task_ids.append(task_id)
            for predicate, has_alias in predicates:
                if has_alias:
                    entry.has_aliased_predicate = True
                candidate = (predicate, task_id)
                if candidate not in entry.predicate_candidates:
                    entry.predicate_candidates.append(candidate)

    tables: list[dict[str, object]] = []
    for entry in table_entries.values():
        unique_predicates = list(
            dict.fromkeys(predicate for predicate, _task_id in entry.predicate_candidates)
        )
        period_predicate = unique_predicates[0] if len(unique_predicates) == 1 else None
        table_manifest: dict[str, object] = {
            "table": entry.table,
            "layer": entry.layer,
            "period_predicate": period_predicate,
            "key": list(KEY_SUGGESTIONS.get(entry.layer, [])),
            "status": (
                "ok"
                if period_predicate is not None
                and not entry.missing_predicate_task_ids
                and not entry.has_aliased_predicate
                and _is_exact_known_pattern(entry.table, entry.layer)
                else "needs_review"
            ),
        }
        if len(unique_predicates) > 1:
            table_manifest["period_predicate_candidates"] = [
                {"predicate": predicate, "task_id": task_id}
                for predicate, task_id in entry.predicate_candidates
            ]
        tables.append(table_manifest)
    for skipped_task in _validation_skipped_tasks(export_dir, flow_id):
        skipped_by_id.setdefault(skipped_task["task_id"], skipped_task)
    return {"flow_id": flow_id, "tables": tables, "skipped": list(skipped_by_id.values())}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate an offline review manifest")
    generate.add_argument("--export-dir", required=True, type=Path)
    generate.add_argument("--flow-id", required=True)
    generate.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "generate":
        raise AssertionError("unreachable command")
    if args.out.exists():
        parser.error(f"output already exists: {args.out}")
    manifest = _generate_manifest(args.export_dir, args.flow_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
