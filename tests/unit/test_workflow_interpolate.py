from __future__ import annotations

import pytest

from app.domain.workflow_interpolate import ParamInterpolationError, interpolate_payload

_VARS = {
    "today": "2026-07-07",
    "now": "2026-07-07T12:30:00",
    "year": "2026",
    "month": "07",
    "day": "07",
}


def test_single_placeholder_substituted() -> None:
    assert interpolate_payload({"table": "t_${today}"}, _VARS) == {"table": "t_2026-07-07"}


def test_multiple_placeholders_in_one_string() -> None:
    result = interpolate_payload({"path": "${year}/${month}/${day}"}, _VARS)
    assert result == {"path": "2026/07/07"}


def test_nested_dict_and_list_recursion() -> None:
    payload = {
        "source_ref": {"table_name": "orders_${today}", "schema": "public"},
        "filters": ["ymd=${year}${month}${day}", "static"],
        "meta": {"nested": {"deep": "${now}"}},
    }
    assert interpolate_payload(payload, _VARS) == {
        "source_ref": {"table_name": "orders_2026-07-07", "schema": "public"},
        "filters": ["ymd=20260707", "static"],
        "meta": {"nested": {"deep": "2026-07-07T12:30:00"}},
    }


def test_no_placeholder_passthrough() -> None:
    payload = {"sql": "SELECT 1", "note": "no vars here"}
    assert interpolate_payload(payload, _VARS) == payload


def test_non_str_values_passthrough() -> None:
    payload = {"limit": 100, "ratio": 1.5, "flag": True, "empty": None, "nums": [1, 2, 3]}
    assert interpolate_payload(payload, _VARS) == payload


def test_does_not_mutate_input() -> None:
    payload = {"table": "t_${today}", "nested": {"x": "${year}"}}
    original = {"table": "t_${today}", "nested": {"x": "${year}"}}
    interpolate_payload(payload, _VARS)
    assert payload == original


def test_missing_variable_raises_with_name_only_never_value() -> None:
    variables = {"secret_token": "super-sensitive-value"}
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "x = ${missing}"}, variables)
    message = str(exc_info.value)
    assert "unresolved_param_variable" in message
    assert "missing" in message
    # ★ R5:错误信息绝不泄露任何变量取值
    assert "super-sensitive-value" not in message


def test_node_reference_unsupported() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${nodes.n1.rows}"}, _VARS)
    assert "unsupported_param_reference" in str(exc_info.value)
    assert "nodes.n1.rows" in str(exc_info.value)


def test_dotted_reference_unsupported() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${foo.bar}"}, _VARS)
    assert "unsupported_param_reference" in str(exc_info.value)


def test_filter_unsupported_in_pr1() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${today | sql_in}"}, _VARS)
    assert "unsupported_param_filter" in str(exc_info.value)
    assert "today | sql_in" in str(exc_info.value)


def test_builtin_date_renders_as_controlled_string_in_sql_and_table() -> None:
    # 注入安全(PR1 by construction):内置日期变量格式受控,渲染进 sql/table_name
    # 后仍是无引号/无分号的确定性字符串
    payload = {
        "source_ref": {"table_name": "snapshot_${today}"},
        "sql": "SELECT * FROM logs WHERE day = '${day}'",
    }
    result = interpolate_payload(payload, _VARS)
    assert result["source_ref"]["table_name"] == "snapshot_2026-07-07"
    assert result["sql"] == "SELECT * FROM logs WHERE day = '07'"
