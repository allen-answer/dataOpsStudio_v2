"""Workflow 节点输出的安全白名单与纯解析工具。"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

from app.domain.result import ResultRef

type NodeOutputValue = str | int | float | bool | None
type NodeOutputMap = Mapping[str, Mapping[str, object]]

COMMON_NODE_OUTPUT_FIELDS = frozenset({"status", "job_id", "error_code"})
NODE_OUTPUT_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "sql_query": COMMON_NODE_OUTPUT_FIELDS | {"result_set_id", "loaded_rows"},
    "sql_explain": COMMON_NODE_OUTPUT_FIELDS | {"result_set_id", "loaded_rows"},
    "compare_run": COMMON_NODE_OUTPUT_FIELDS
    | {"run_id", "same_count", "only_source_count", "only_target_count", "diff_count"},
    "lineage_analyze": COMMON_NODE_OUTPUT_FIELDS
    | {"run_id", "cached", "table_edge_count", "column_edge_count", "parse_error_count"},
    "export_excel": COMMON_NODE_OUTPUT_FIELDS,
    "notify": COMMON_NODE_OUTPUT_FIELDS | {"sent_count"},
    "sleep": COMMON_NODE_OUTPUT_FIELDS | {"duration_seconds"},
    "branch": COMMON_NODE_OUTPUT_FIELDS | {"selected_target"},
}
ALL_NODE_OUTPUT_FIELDS = frozenset(
    field for fields in NODE_OUTPUT_FIELDS_BY_KIND.values() for field in fields
)

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
_NODE_OUTPUT_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class NodeOutputReferenceError(ValueError):
    """安全节点输出引用无法解析;消息与 code 属性仅含稳定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def extract_workflow_node_outputs(
    job_kind: str,
    result_ref: ResultRef | None,
) -> dict[str, NodeOutputValue]:
    """Project a child ResultRef through the Workflow scalar output whitelist."""
    if result_ref is None:
        return {}
    metadata = result_ref.metadata
    outputs: dict[str, NodeOutputValue] = {}
    if job_kind in {"sql_query", "sql_explain"}:
        _copy_string(metadata, "result_set_id", outputs)
        _copy_int(metadata, "loaded_rows", outputs)
    elif job_kind == "compare_run":
        _copy_string(metadata, "run_id", outputs)
        bucket_counts = metadata.get("bucket_counts")
        if isinstance(bucket_counts, Mapping):
            for bucket in ("same", "only_source", "only_target", "diff"):
                _copy_int(bucket_counts, bucket, outputs, target=f"{bucket}_count")
    elif job_kind == "lineage_analyze":
        _copy_string(metadata, "lineage_run_id", outputs, target="run_id")
        _copy_bool(metadata, "cached", outputs)
        for field_name in ("table_edge_count", "column_edge_count", "parse_error_count"):
            _copy_int(metadata, field_name, outputs)
    elif job_kind == "branch":
        _copy_string(metadata, "selected_target", outputs)
    elif job_kind == "sleep":
        _copy_int(metadata, "duration_seconds", outputs)
    elif job_kind == "notify":
        _copy_int(metadata, "sent_count", outputs)
    return outputs


def _copy_string(
    source: Mapping[str, object],
    field_name: str,
    outputs: dict[str, NodeOutputValue],
    *,
    target: str | None = None,
) -> None:
    value = source.get(field_name)
    if isinstance(value, str) and value:
        outputs[target or field_name] = value


def _copy_int(
    source: Mapping[str, object],
    field_name: str,
    outputs: dict[str, NodeOutputValue],
    *,
    target: str | None = None,
) -> None:
    value = source.get(field_name)
    if isinstance(value, int) and not isinstance(value, bool):
        outputs[target or field_name] = value


def _copy_bool(
    source: Mapping[str, object],
    field_name: str,
    outputs: dict[str, NodeOutputValue],
) -> None:
    value = source.get(field_name)
    if isinstance(value, bool):
        outputs[field_name] = value


def parse_node_output_reference(expression: str) -> tuple[str, str] | None:
    candidate = expression.strip()
    if not candidate.startswith("nodes."):
        return None
    node_id, separator, field = candidate.removeprefix("nodes.").rpartition(".")
    if not separator or not node_id or _NODE_OUTPUT_FIELD_RE.fullmatch(field) is None:
        return None
    return node_id, field


def resolve_node_output(expression: str, outputs: NodeOutputMap) -> NodeOutputValue:
    reference = parse_node_output_reference(expression)
    if reference is None:
        raise NodeOutputReferenceError("invalid_node_output_reference")
    node_id, field = reference
    if field not in ALL_NODE_OUTPUT_FIELDS:
        raise NodeOutputReferenceError("forbidden_node_output_field")
    node_outputs = outputs.get(node_id)
    if node_outputs is None or field not in node_outputs:
        raise NodeOutputReferenceError("unresolved_node_output")
    value = node_outputs[field]
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise NodeOutputReferenceError("unsafe_node_output_value")
    return value


def iter_placeholder_expressions(value: object) -> Iterator[str]:
    if isinstance(value, str):
        for match in _PLACEHOLDER_RE.finditer(value):
            yield match.group(1).strip()
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from iter_placeholder_expressions(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_placeholder_expressions(item)
