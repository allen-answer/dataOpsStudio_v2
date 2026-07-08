from __future__ import annotations

from app.domain.ai_copilot import (
    build_nl2sql_prompt,
    build_schema_context,
    split_sql_and_explanation,
)
from app.domain.schema import Column, ColumnType


def _cols(n: int) -> list[Column]:
    return [Column(name=f"c{i}", type=ColumnType.INTEGER) for i in range(n)]


def test_build_schema_context_emits_structure_only_no_row_values() -> None:
    tables = [
        (
            "app",
            "users",
            [
                Column(name="id", type=ColumnType.INTEGER, primary_key=True, nullable=False),
                Column(name="email", type=ColumnType.STRING, comment="login"),
            ],
        )
    ]

    payload, tables_used, truncated = build_schema_context(tables)

    assert truncated is False
    assert tables_used == ["app.users"]
    assert payload == [
        {
            "schema": "app",
            "table": "users",
            "columns": [
                {
                    "name": "id",
                    "type": "integer",
                    "nullable": False,
                    "primary_key": True,
                    "comment": None,
                },
                {
                    "name": "email",
                    "type": "string",
                    "nullable": True,
                    "primary_key": False,
                    "comment": "login",
                },
            ],
        }
    ]
    # ★ egress L2 guard: only structural keys; no "value"/"sample"/row-data keys.
    for entry in payload:
        for col in entry["columns"]:
            assert set(col.keys()) == {"name", "type", "nullable", "primary_key", "comment"}


def test_build_schema_context_truncates_tables_and_columns() -> None:
    tables = [(f"s{i}", f"t{i}", _cols(2)) for i in range(20)]

    payload, tables_used, truncated = build_schema_context(tables, max_tables=5)

    assert truncated is True
    assert len(payload) == 5
    assert len(tables_used) == 5

    wide = [("app", "wide", _cols(100))]
    _, _, col_truncated = build_schema_context(wide, max_columns_per_table=10)
    assert col_truncated is True


def test_split_sql_and_explanation_prefers_fenced_block() -> None:
    content = "Here is the query:\n```sql\nSELECT 1;\n```\nIt returns a constant."
    sql, explanation = split_sql_and_explanation(content)
    assert sql == "SELECT 1;"
    assert explanation is not None
    assert "Here is the query" in explanation


def test_split_sql_and_explanation_falls_back_to_whole_content() -> None:
    sql, explanation = split_sql_and_explanation("ok")
    assert sql == "ok"
    assert explanation is None


def test_build_nl2sql_prompt_includes_request_and_dialect() -> None:
    prompt = build_nl2sql_prompt("top customers", dialect="mysql")
    assert "top customers" in prompt
    assert "mysql" in prompt
    assert "read-only" in prompt
