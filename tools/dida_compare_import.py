"""Build reviewable compare manifests from a parsed DIDA export."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml  # type: ignore[import-untyped]

from app.domain.workflow import MAX_WHEN_LENGTH, MAX_WORKFLOW_NODES

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
QUALIFIED_TABLE_PATTERN = re.compile(
    r"^(?P<schema>[A-Za-z_][A-Za-z0-9_$#]*)\.(?P<table>[A-Za-z_][A-Za-z0-9_$#]*)$"
)
DEFAULT_IGNORE_COLUMNS: tuple[str, ...] = ("indices_opt",)
API_PAGE_SIZE = 500


@dataclass
class _TableEntry:
    table: str
    layer: str
    predicate_candidates: list[tuple[str, str]] = field(default_factory=list)
    missing_predicate_task_ids: list[str] = field(default_factory=list)
    has_aliased_predicate: bool = False


@dataclass(frozen=True)
class ApplyReport:
    tasks_created: int
    tasks_updated: int
    needs_review_skipped: int
    workflow_created: bool
    workflow_updated: bool


@dataclass(frozen=True)
class _ApplyTable:
    table: str
    schema_name: str
    table_name: str
    period_predicate: str
    keys: tuple[str, ...]


class JsonApi(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> Any: ...


class UrlLibJsonApi:
    def __init__(self, base_url: str, credential: str, *, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> Any:
        api_path = (
            path[4:] if self._base_url.endswith("/api") and path.startswith("/api/") else path
        )
        url = f"{self._base_url}{api_path}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._credential}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            error_code = "unknown_error"
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                if isinstance(error_payload, dict) and isinstance(error_payload.get("error"), str):
                    error_code = error_payload["error"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise RuntimeError(
                f"API {method} {path.split('?', maxsplit=1)[0]} failed: "
                f"HTTP {exc.code} ({error_code})"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError(
                f"API {method} {path.split('?', maxsplit=1)[0]} request failed"
            ) from None
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"API {method} {path.split('?', maxsplit=1)[0]} returned invalid JSON"
            ) from exc


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


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _objects(value: object, *, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [_object(item, label=f"{label} item") for item in value]


def _nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _apply_tables(manifest: Mapping[str, object]) -> tuple[str, list[_ApplyTable], int]:
    flow_id = _nonempty_string(manifest.get("flow_id"), label="flow_id")
    tables = _objects(manifest.get("tables"), label="tables")
    applicable: list[_ApplyTable] = []
    needs_review = 0
    for entry in tables:
        status = entry.get("status")
        if status == "needs_review":
            needs_review += 1
            continue
        if status != "ok":
            raise ValueError("table status must be ok or needs_review")
        qualified_table = _nonempty_string(entry.get("table"), label="table")
        match = QUALIFIED_TABLE_PATTERN.fullmatch(qualified_table)
        if match is None:
            raise ValueError("ok table must be a safe schema.table identifier")
        predicate = _nonempty_string(
            entry.get("period_predicate"), label=f"period_predicate for {qualified_table}"
        )
        raw_keys = entry.get("key")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise ValueError(f"key for {qualified_table} must be a non-empty list")
        keys = tuple(_nonempty_string(key, label=f"key for {qualified_table}") for key in raw_keys)
        applicable.append(
            _ApplyTable(
                table=qualified_table,
                schema_name=match.group("schema"),
                table_name=match.group("table"),
                period_predicate=predicate,
                keys=keys,
            )
        )
    if not applicable:
        raise ValueError("manifest has no status=ok tables to apply")
    return flow_id, applicable, needs_review


def _managed_name(prefix: str | None, suffix: str) -> str:
    name = f"{prefix}|{suffix}" if prefix else suffix
    if len(name) > 128:
        raise ValueError("generated name exceeds 128 characters")
    return name


def _collection_snapshot(api: JsonApi, path: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    offset = 0
    separator = "&" if "?" in path else "?"
    while True:
        page = _objects(
            api.request_json(
                "GET",
                f"{path}{separator}limit={API_PAGE_SIZE}&offset={offset}",
            ),
            label="API collection response",
        )
        rows.extend(page)
        if len(page) < API_PAGE_SIZE:
            return rows
        offset += len(page)


def _by_unique_name(rows: Sequence[Mapping[str, object]], name: str) -> dict[str, object] | None:
    matches = [dict(row) for row in rows if row.get("name") == name]
    if len(matches) > 1:
        raise ValueError(f"multiple API resources use managed name: {name}")
    return matches[0] if matches else None


def _resolve_column_name(
    requested: str,
    columns: Sequence[Mapping[str, object]],
    *,
    required: bool,
) -> str | None:
    matches = [
        name
        for column in columns
        if isinstance((name := column.get("name")), str) and name.casefold() == requested.casefold()
    ]
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise ValueError(f"ambiguous inferred column: {requested}")
    if not unique:
        if required:
            raise ValueError(f"missing inferred key column: {requested}")
        return None
    return unique[0]


def _mapping_for_key(
    requested: str,
    mappings: Sequence[Mapping[str, object]],
) -> tuple[str, str, str]:
    matches: list[tuple[str, str, str]] = []
    for mapping in mappings:
        source = mapping.get("source_column")
        target = mapping.get("target_column")
        if (
            not isinstance(source, str)
            or source.casefold() != requested.casefold()
            or not isinstance(target, str)
        ):
            continue
        source_type = mapping.get("source_type")
        matches.append(
            (
                source,
                target,
                source_type if isinstance(source_type, str) else "unknown",
            )
        )
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise ValueError(f"ambiguous inferred key mapping: {requested}")
    if not unique:
        raise ValueError(f"missing inferred key column: {requested}")
    return unique[0]


def _mapping_source_type(
    source: str,
    target: str,
    mappings: Sequence[Mapping[str, object]],
) -> str:
    types = {
        source_type
        for mapping in mappings
        if mapping.get("source_column") == source
        and mapping.get("target_column") == target
        and isinstance((source_type := mapping.get("source_type")), str)
    }
    return next(iter(types)) if len(types) == 1 else "unknown"


def _task_body_from_inference(
    table: _ApplyTable,
    inference: Mapping[str, object],
    *,
    project_id: str,
    source_id: str,
    target_id: str,
    name: str,
    max_rows: int | None,
) -> dict[str, object]:
    value_columns = _objects(inference.get("columns"), label="compare inference columns")
    mappings = _objects(inference.get("mappings"), label="compare inference mappings")
    key_mappings = [_mapping_for_key(key, mappings) for key in table.keys]
    resolved_keys = [source for source, _target, _source_type in key_mappings]
    rules = _object(inference.get("compare_rules"), label="compare inference rules").copy()
    raw_column_mappings = rules.get("column_mappings", {})
    if not isinstance(raw_column_mappings, dict):
        raise ValueError("compare inference column_mappings must be an object")
    column_mappings = dict(raw_column_mappings)
    key_columns = [
        {"name": source, "type": source_type, "primary_key": True}
        for source, _target, source_type in key_mappings
    ]
    key_names = {name.casefold() for name in resolved_keys}
    columns = [
        *key_columns,
        *[
            column
            for column in value_columns
            if not (
                isinstance(column.get("name"), str) and str(column["name"]).casefold() in key_names
            )
        ],
    ]
    configured_names = {
        str(column["name"]).casefold() for column in columns if isinstance(column.get("name"), str)
    }
    for source, target in column_mappings.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("compare inference column_mappings must map strings to strings")
        if source.casefold() in configured_names:
            continue
        columns.append(
            {
                "name": source,
                "type": _mapping_source_type(source, target, mappings),
            }
        )
        configured_names.add(source.casefold())
    if not columns:
        raise ValueError(f"compare inference returned no columns for {table.table}")
    ignore_columns = [
        resolved
        for requested in DEFAULT_IGNORE_COLUMNS
        if (resolved := _resolve_column_name(requested, columns, required=False)) is not None
    ]
    column_mappings.update({source: target for source, target, _source_type in key_mappings})
    rules.update(
        {
            "key_columns": resolved_keys,
            "ignore_columns": ignore_columns,
            "column_mappings": column_mappings,
            "empty_as_null": True,
            "trim_strings": True,
            "schema_policy": "warn",
        }
    )
    sql = f"select * from {table.table} where {table.period_predicate}"
    run_limits: dict[str, object] = {}
    if max_rows is not None:
        run_limits["max_rows"] = max_rows
    return {
        "project_id": project_id,
        "name": name,
        "source_id": source_id,
        "target_id": target_id,
        "source_ref": {"kind": "sql", "sql": sql},
        "target_ref": {"kind": "sql", "sql": sql},
        "columns": columns,
        "compare_rules": rules,
        "run_limits": run_limits,
    }


def _workflow_compare_payload(task: Mapping[str, object]) -> dict[str, object]:
    task_id = _nonempty_string(task.get("id"), label="compare task id")
    payload: dict[str, object] = {"task_id": task_id}
    for field_name in (
        "source_id",
        "target_id",
        "source_ref",
        "target_ref",
        "columns",
        "compare_rules",
        "run_limits",
    ):
        if field_name not in task:
            raise ValueError(f"compare task response missing {field_name}")
        payload[field_name] = task[field_name]
    return payload


def _query_timeout(task: Mapping[str, object]) -> int:
    limits = _object(task.get("run_limits"), label="compare task run_limits")
    value = limits.get("query_timeout_seconds", 1800)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1800


def _diff_condition(node_id: str) -> str:
    return "||".join(
        f"${{nodes.{node_id}.{field_name}}}>0"
        for field_name in ("diff_count", "only_source_count", "only_target_count")
    )


def _group_diff_conditions(node_ids: Sequence[str]) -> list[str]:
    groups: list[str] = []
    current = ""
    for node_id in node_ids:
        condition = _diff_condition(node_id)
        candidate = f"{current}||{condition}" if current else condition
        if len(candidate) <= MAX_WHEN_LENGTH:
            current = candidate
            continue
        if not current:
            raise ValueError("single compare notification condition exceeds workflow limit")
        groups.append(current)
        current = condition
    if current:
        groups.append(current)
    return groups


def _notification_target(spec: Mapping[str, object], target_id: str) -> dict[str, object] | None:
    for target in _objects(spec.get("notifications", []), label="workflow notifications"):
        if target.get("id") == target_id:
            return target
    return None


def apply_manifest(
    manifest: Mapping[str, object],
    *,
    project_id: str,
    source_id: str,
    target_id: str,
    prefix: str | None,
    notify_target: str | None,
    max_rows: int | None,
    api: JsonApi,
) -> ApplyReport:
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be greater than zero")
    flow_id, tables, needs_review = _apply_tables(manifest)
    condition_groups = (
        _group_diff_conditions([f"c{index}" for index in range(len(tables))])
        if notify_target is not None
        else []
    )
    required_nodes = (
        len(tables) + 1 + len(condition_groups) + 1 if notify_target is not None else len(tables)
    )
    if required_nodes > MAX_WORKFLOW_NODES:
        raise ValueError(
            "workflow exceeds MAX_WORKFLOW_NODES: "
            f"requires {required_nodes}, limit is {MAX_WORKFLOW_NODES}"
        )
    task_names = [_managed_name(prefix, table.table) for table in tables]
    workflow_name = _managed_name(prefix, flow_id)
    existing_tasks = _collection_snapshot(
        api,
        f"/api/compare/tasks?project_id={project_id}",
    )
    existing_workflows = _collection_snapshot(
        api,
        f"/api/projects/{project_id}/workflows",
    )
    existing_workflow = _by_unique_name(existing_workflows, workflow_name)
    workflow_detail: dict[str, object] | None = None
    existing_spec: dict[str, object] | None = None
    if notify_target is not None:
        if existing_workflow is None:
            raise ValueError(
                "notify_target requires an existing workflow notification target; "
                "create the workflow, configure its target, then re-run apply"
            )
        workflow_id = _nonempty_string(existing_workflow.get("id"), label="existing workflow id")
        workflow_detail = _object(
            api.request_json(
                "GET",
                f"/api/projects/{project_id}/workflows/{workflow_id}",
            ),
            label="workflow response",
        )
        existing_spec = _object(workflow_detail.get("spec"), label="workflow spec")
        target = _notification_target(existing_spec, notify_target)
        if target is None:
            raise ValueError("existing workflow notification target was not found")
        if target.get("enabled", True) is False:
            raise ValueError("existing workflow notification target is disabled")
    task_plans: list[tuple[str, dict[str, object]]] = []
    for table, task_name in zip(tables, task_names, strict=True):
        inference = _object(
            api.request_json(
                "POST",
                f"/api/projects/{project_id}/compare/infer",
                body={
                    "source_id": source_id,
                    "target_id": target_id,
                    "source_table": {
                        "schema_name": table.schema_name,
                        "table_name": table.table_name,
                    },
                    "target_table": {
                        "schema_name": table.schema_name,
                        "table_name": table.table_name,
                    },
                },
            ),
            label="compare inference response",
        )
        task_plans.append(
            (
                task_name,
                _task_body_from_inference(
                    table,
                    inference,
                    project_id=project_id,
                    source_id=source_id,
                    target_id=target_id,
                    name=task_name,
                    max_rows=max_rows,
                ),
            )
        )
    applied_tasks: list[dict[str, object]] = []
    tasks_created = 0
    tasks_updated = 0
    for task_name, body in task_plans:
        existing_task = _by_unique_name(existing_tasks, task_name)
        if existing_task is None:
            method = "POST"
            path = "/api/compare/tasks"
            request_body = body
            tasks_created += 1
        else:
            task_id = _nonempty_string(existing_task.get("id"), label="existing compare task id")
            method = "PATCH"
            path = f"/api/compare/tasks/{task_id}"
            request_body = {key: value for key, value in body.items() if key != "project_id"}
            tasks_updated += 1
        applied_tasks.append(
            _object(
                api.request_json(method, path, body=request_body),
                label="compare task response",
            )
        )
    nodes: list[dict[str, object]] = [
        {
            "id": f"c{index}",
            "job_kind": "compare_run",
            "payload": _workflow_compare_payload(task),
            "timeout_seconds": _query_timeout(task),
        }
        for index, task in enumerate(applied_tasks)
    ]
    edges: list[dict[str, object]] = []
    if notify_target is not None:
        nodes.append(
            {
                "id": "branch",
                "job_kind": "branch",
                "payload": {},
                "timeout_seconds": 60,
            }
        )
        nodes.extend(
            {
                "id": f"notify{index}",
                "job_kind": "notify",
                "payload": {
                    "target_ids": [notify_target],
                    "message": "DIDA compare differences detected",
                },
                "timeout_seconds": 60,
            }
            for index in range(len(condition_groups))
        )
        nodes.append(
            {
                "id": "no_diff",
                "job_kind": "sleep",
                "payload": {"duration_seconds": 1},
                "timeout_seconds": 60,
            }
        )
        edges.extend({"source": f"c{index}", "target": "branch"} for index in range(len(tables)))
        edges.extend(
            {
                "source": "branch",
                "target": f"notify{index}",
                "when": condition,
            }
            for index, condition in enumerate(condition_groups)
        )
        edges.append({"source": "branch", "target": "no_diff", "is_default": True})
    workflow_spec: dict[str, object] = {"nodes": nodes, "edges": edges}
    workflow_body: dict[str, object] = {
        "name": workflow_name,
        "spec": workflow_spec,
    }
    if existing_workflow is None:
        workflow_method = "POST"
        workflow_path = f"/api/projects/{project_id}/workflows"
        workflow_created = True
    else:
        workflow_id = _nonempty_string(existing_workflow.get("id"), label="existing workflow id")
        if workflow_detail is None:
            workflow_detail = _object(
                api.request_json(
                    "GET",
                    f"/api/projects/{project_id}/workflows/{workflow_id}",
                ),
                label="workflow response",
            )
        if existing_spec is None:
            existing_spec = _object(workflow_detail.get("spec"), label="workflow spec")
        # Workflow PUT preserves notifications/variables when omitted. Keep those
        # server-side so SecretRefs are never copied into this CLI request.
        for field_name in ("schedule", "sensor"):
            if field_name in existing_spec:
                workflow_spec[field_name] = existing_spec[field_name]
        workflow_method = "PUT"
        workflow_path = f"/api/projects/{project_id}/workflows/{workflow_id}"
        workflow_created = False
    api.request_json(workflow_method, workflow_path, body=workflow_body)
    return ApplyReport(
        tasks_created=tasks_created,
        tasks_updated=tasks_updated,
        needs_review_skipped=needs_review,
        workflow_created=workflow_created,
        workflow_updated=not workflow_created,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate an offline review manifest")
    generate.add_argument("--export-dir", required=True, type=Path)
    generate.add_argument("--flow-id", required=True)
    generate.add_argument("--out", required=True, type=Path)
    apply = subparsers.add_parser("apply", help="apply a reviewed manifest through the API")
    apply.add_argument("--manifest", required=True, type=Path)
    apply.add_argument("--project", required=True)
    apply.add_argument("--source-ds", required=True)
    apply.add_argument("--target-ds", required=True)
    apply.add_argument("--prefix")
    apply.add_argument("--notify-target")
    apply.add_argument("--max-rows", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        if args.out.exists():
            parser.error(f"output already exists: {args.out}")
        manifest = _generate_manifest(args.export_dir, args.flow_id)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return 0
    if args.command == "apply":
        if not args.manifest.is_file():
            parser.error(f"manifest does not exist: {args.manifest}")
        raw_manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        api_url = os.environ.get("DATAOPS_API_URL")
        credential = os.environ.get("DATAOPS_API_TOKEN")
        if not api_url:
            parser.error("DATAOPS_API_URL is required")
        if not credential:
            parser.error("DATAOPS_API_TOKEN is required")
        try:
            report = apply_manifest(
                _object(raw_manifest, label="manifest"),
                project_id=args.project,
                source_id=args.source_ds,
                target_id=args.target_ds,
                prefix=args.prefix,
                notify_target=args.notify_target,
                max_rows=args.max_rows,
                api=UrlLibJsonApi(api_url, credential),
            )
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
