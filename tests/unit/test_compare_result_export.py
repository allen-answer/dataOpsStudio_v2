"""compare 结果导出:解码业务列(pk + source/target 值)而非原始 4 列 JSON blob。

覆盖 backlog #96 评论第 2 条落地:列头/行值正确、diff 并排标注、复杂值序列化、
公式注入防御经写入层仍生效、空桶 sheet 据全 run schema 产出正确表头。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from app.domain.compare import CompareDiffBucket
from app.domain.compare_result import (
    compare_export_columns,
    compare_export_row,
    compare_export_schema,
    decode_compare_result_row,
    encode_compare_result_row,
)
from app.infrastructure.result_export import write_xlsx_workbook

ONLY_SOURCE = CompareDiffBucket.ONLY_SOURCE.value
ONLY_TARGET = CompareDiffBucket.ONLY_TARGET.value
DIFF = CompareDiffBucket.DIFF.value
SAME = CompareDiffBucket.SAME.value


def _decoded(
    pk: dict[str, Any],
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    cells: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return decode_compare_result_row(
        encode_compare_result_row(pk=pk, source=source, target=target, cells=cells or [])
    )


def test_schema_derives_key_and_value_columns() -> None:
    decoded = _decoded({"id": 1}, {"name": "a", "amount": 10}, {"name": "b", "amount": 20})
    key_columns, value_columns = compare_export_schema(decoded)
    assert key_columns == ["id"]
    assert value_columns == ["name", "amount"]


def test_schema_unions_source_and_target_value_keys_in_order() -> None:
    # 某侧独有的列不漏(并集保序)。
    decoded = _decoded({"k": 1}, {"a": 1}, {"b": 2})
    key_columns, value_columns = compare_export_schema(decoded)
    assert key_columns == ["k"]
    assert value_columns == ["a", "b"]


def test_columns_non_diff_bucket_is_key_plus_value_single_columns() -> None:
    columns = compare_export_columns(ONLY_SOURCE, ["id"], ["name", "amount"])
    assert [c.name for c in columns] == ["id", "name", "amount"]


def test_columns_diff_bucket_pairs_source_and_target_per_value_column() -> None:
    columns = compare_export_columns(DIFF, ["id"], ["name", "amount"])
    assert [c.name for c in columns] == [
        "id",
        "name (source)",
        "name (target)",
        "amount (source)",
        "amount (target)",
    ]


def test_row_only_source_uses_source_side() -> None:
    decoded = _decoded({"id": 7}, {"name": "alice", "amount": 3}, None)
    row = compare_export_row(ONLY_SOURCE, decoded, ["id"], ["name", "amount"])
    assert row.values == [7, "alice", 3]


def test_row_only_target_uses_target_side() -> None:
    decoded = _decoded({"id": 7}, None, {"name": "bob", "amount": 9})
    row = compare_export_row(ONLY_TARGET, decoded, ["id"], ["name", "amount"])
    assert row.values == [7, "bob", 9]


def test_row_same_uses_source_side_equal_to_target() -> None:
    decoded = _decoded({"id": 1}, {"name": "x"}, {"name": "x"})
    row = compare_export_row(SAME, decoded, ["id"], ["name"])
    assert row.values == [1, "x"]


def test_row_diff_places_source_and_target_side_by_side() -> None:
    decoded = _decoded(
        {"id": 1},
        {"name": "old", "amount": 10},
        {"name": "new", "amount": 10},
    )
    row = compare_export_row(DIFF, decoded, ["id"], ["name", "amount"])
    # 每值列输出 source、target 两格;相同列(amount)两格相等,差异列(name)两格不同。
    assert row.values == [1, "old", "new", 10, 10]


def test_row_complex_value_is_json_encoded_scalars_pass_through() -> None:
    decoded = _decoded({"id": 1}, {"payload": {"k": [1, 2]}, "flag": True}, None)
    row = compare_export_row(ONLY_SOURCE, decoded, ["id"], ["payload", "flag"])
    # 标量(bool/int)原样保留;嵌套结构压成紧凑 JSON 串。
    assert row.values == [1, '{"k":[1,2]}', True]


def test_missing_value_column_yields_none() -> None:
    decoded = _decoded({"id": 1}, {"name": "a"}, None)
    row = compare_export_row(ONLY_SOURCE, decoded, ["id"], ["name", "missing"])
    assert row.values == [1, "a", None]


def test_end_to_end_injection_defense_applied_by_writer() -> None:
    # 危险公式前缀值经写入层 sanitize 加撇号转义为文本,导出不再是可执行公式。
    decoded = _decoded({"id": 1}, {"name": "=cmd|' /C calc'!A0"}, None)
    columns = compare_export_columns(ONLY_SOURCE, ["id"], ["name"])
    row = compare_export_row(ONLY_SOURCE, decoded, ["id"], ["name"])
    stream = io.BytesIO()
    write_xlsx_workbook(
        stream=stream,
        sheets=[(ONLY_SOURCE, columns, [row])],
        limit_bytes=1024 * 1024,
    )
    stream.seek(0)
    with zipfile.ZipFile(stream) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "'=cmd" in sheet_xml  # 前置撇号 → 文本,非公式


def test_empty_bucket_still_gets_business_headers_from_run_schema() -> None:
    # 空桶无行,但据全 run schema(key=id,value=name)仍产出正确表头。
    columns = compare_export_columns(SAME, ["id"], ["name"])
    stream = io.BytesIO()
    write_xlsx_workbook(
        stream=stream,
        sheets=[(SAME, columns, [])],
        limit_bytes=1024 * 1024,
    )
    stream.seek(0)
    with zipfile.ZipFile(stream) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<t>id</t>" in sheet_xml
    assert "<t>name</t>" in sheet_xml
    assert "pk/source/target/cells" not in sheet_xml
