from __future__ import annotations

from app.domain.lineage.cache import lineage_sql_hash


def test_lineage_sql_hash_includes_sql_dialect_schema_and_parser_version() -> None:
    base = lineage_sql_hash(
        sql_text="INSERT INTO tgt SELECT id FROM src",
        dialect="mysql",
        schema_context={"schema": {"app": {"src": {"id": "integer"}}}},
        parser_version="v1",
    )

    assert (
        lineage_sql_hash(
            sql_text="INSERT INTO tgt SELECT id FROM src",
            dialect="mysql",
            schema_context={"schema": {"app": {"src": {"id": "integer"}}}},
            parser_version="v1",
        )
        == base
    )
    assert (
        lineage_sql_hash(
            sql_text="INSERT INTO tgt SELECT amount FROM src",
            dialect="mysql",
            schema_context={"schema": {"app": {"src": {"id": "integer"}}}},
            parser_version="v1",
        )
        != base
    )
    assert (
        lineage_sql_hash(
            sql_text="INSERT INTO tgt SELECT id FROM src",
            dialect="dm",
            schema_context={"schema": {"app": {"src": {"id": "integer"}}}},
            parser_version="v1",
        )
        != base
    )
    assert (
        lineage_sql_hash(
            sql_text="INSERT INTO tgt SELECT id FROM src",
            dialect="mysql",
            schema_context={"schema": {"app": {"src": {"id": "integer", "amount": "decimal"}}}},
            parser_version="v1",
        )
        != base
    )
    assert (
        lineage_sql_hash(
            sql_text="INSERT INTO tgt SELECT id FROM src",
            dialect="mysql",
            schema_context={"schema": {"app": {"src": {"id": "integer"}}}},
            parser_version="v2",
        )
        != base
    )
