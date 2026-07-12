"""Workflow 节点输出的安全白名单与纯解析工具。"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

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
