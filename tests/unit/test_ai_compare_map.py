from __future__ import annotations

from app.domain.ai_compare_map import (
    aggregate_mapping_history,
    build_map_suggest_prompt,
    column_schema_payload,
    parse_map_suggestions,
    project_sample_rows,
    split_residual_columns,
)
from app.domain.schema import Column, ColumnType


def _col(name: str, ctype: ColumnType = ColumnType.STRING) -> Column:
    return Column(name=name, type=ctype)


def test_split_residual_columns_removes_confirmed_case_insensitive() -> None:
    source = [_col("id", ColumnType.INTEGER), _col("amount", ColumnType.DECIMAL), _col("note")]
    target = [_col("ID", ColumnType.INTEGER), _col("amount", ColumnType.DECIMAL), _col("memo")]
    # 已确认映射(规则推断后):id->ID、amount->amount;残余只剩 note / memo。
    residual_source, residual_target, truncated = split_residual_columns(
        source, target, {"id": "ID", "amount": "amount"}
    )
    assert [c.name for c in residual_source] == ["note"]
    assert [c.name for c in residual_target] == ["memo"]
    assert truncated is False


def test_column_schema_payload_is_structure_only_no_row_values() -> None:
    payload = column_schema_payload(
        [Column(name="memo", type=ColumnType.STRING, nullable=False, comment="note")]
    )
    assert payload == [
        {
            "name": "memo",
            "type": "string",
            "nullable": False,
            "primary_key": False,
            "comment": "note",
        }
    ]
    # ★ egress L2 guard:只有结构键,没有 value/sample/行数据键。
    assert set(payload[0]) == {"name", "type", "nullable", "primary_key", "comment"}


def test_aggregate_mapping_history_counts_and_orders_by_frequency() -> None:
    history = aggregate_mapping_history(
        [
            {"note": "memo", "qty": "quantity"},
            {"note": "memo"},
            {"": "memo", "qty": ""},  # 空键/空值忽略
        ]
    )
    assert history[0] == {"source_column": "note", "target_column": "memo", "times_confirmed": 2}
    assert {"source_column": "qty", "target_column": "quantity", "times_confirmed": 1} in history
    assert all(item["source_column"] for item in history)


def test_aggregate_mapping_history_empty_returns_empty() -> None:
    assert aggregate_mapping_history([]) == []
    assert aggregate_mapping_history([{}]) == []


def test_build_prompt_no_history_tells_ai_no_history_and_not_fabricate() -> None:
    prompt = build_map_suggest_prompt(
        source_dialect="mysql", target_dialect="oracle", has_history=False, include_samples=False
    )
    assert "No historical mapping decisions are available" in prompt
    assert "do not fabricate history" in prompt
    assert "No sample values are provided" in prompt
    assert "mysql" in prompt and "oracle" in prompt


def test_build_prompt_with_history_and_samples() -> None:
    prompt = build_map_suggest_prompt(
        source_dialect="mysql", target_dialect="mysql", has_history=True, include_samples=True
    )
    assert "Historical confirmed mappings" in prompt
    assert "Sample row values" in prompt


def test_project_sample_rows_keeps_only_residual_columns_and_truncates() -> None:
    columns = ["id", "note", "secret_blob"]
    rows = [[1, "hello", "x" * 500], [2, "world", "y"]]
    projected = project_sample_rows(columns, rows, ["note"], max_rows=1, max_cell_len=10)
    # 只保留残余列 note;主键/其他列不出站;≤1 行;单元格截断。
    assert projected["columns"] == ["note"]
    assert projected["rows"] == [["hello"]]


def test_parse_map_suggestions_reads_fenced_json_and_filters_invented_columns() -> None:
    content = (
        "Here you go:\n```json\n"
        '{"suggestions": ['
        '{"source_column": "note", "target_column": "memo", "confidence": 0.9, '
        '"rationale": "same text shape"},'
        '{"source_column": "ghost", "target_column": "memo", "confidence": 0.5}'
        "]}\n```"
    )
    out = parse_map_suggestions(content, valid_sources={"note"}, valid_targets={"memo"})
    assert len(out) == 1  # ghost 不在残余源列 → 丢弃(绝不臆造列)
    assert out[0].source_column == "note"
    assert out[0].target_column == "memo"
    assert out[0].confidence == 0.9
    assert out[0].rationale == "same text shape"


def test_parse_map_suggestions_clamps_confidence_and_dedupes_source() -> None:
    content = (
        '[{"source_column": "note", "target_column": "memo", "confidence": 5},'
        '{"source_column": "note", "target_column": "other", "confidence": 0.1}]'
    )
    out = parse_map_suggestions(content, valid_sources={"note"}, valid_targets={"memo", "other"})
    assert len(out) == 1  # 同源列去重,取第一条
    assert out[0].confidence == 1.0  # 越界置信度夹到 [0,1]


def test_parse_map_suggestions_unparseable_returns_empty() -> None:
    # MockProvider 回 "ok" 走这支:不可解析为 JSON → 空建议(route 层 ok=True)。
    assert parse_map_suggestions("ok", valid_sources={"note"}, valid_targets={"memo"}) == []
