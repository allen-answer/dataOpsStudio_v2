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


def test_node_output_exact_placeholder_preserves_scalar_type() -> None:
    outputs = {"n1": {"loaded_rows": 7, "cached": False, "error_code": None}}
    assert interpolate_payload(
        {
            "rows": "${nodes.n1.loaded_rows}",
            "cached": "${nodes.n1.cached}",
            "error": "${nodes.n1.error_code}",
        },
        _VARS,
        node_outputs=outputs,
    ) == {"rows": 7, "cached": False, "error": None}


def test_node_output_embedded_placeholder_stringifies_scalar() -> None:
    result = interpolate_payload(
        {"source": "result-${nodes.query.result_set_id}"},
        _VARS,
        node_outputs={"query": {"result_set_id": "rs-1"}},
    )
    assert result == {"source": "result-rs-1"}


@pytest.mark.parametrize("node_id", ["extract.v1", "导出"])
def test_node_output_reference_supports_legacy_nonblank_node_ids(node_id: str) -> None:
    placeholder = f"${{nodes.{node_id}.loaded_rows}}"

    result = interpolate_payload(
        {"rows": placeholder},
        _VARS,
        node_outputs={node_id: {"loaded_rows": 7}},
    )

    assert result == {"rows": 7}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("${nodes.a|b.loaded_rows}", 7),
        ("rows=${nodes.a|b.loaded_rows}", "rows=7"),
    ],
    ids=["exact", "embedded"],
)
def test_pipe_in_legacy_node_id_is_not_treated_as_filter(
    template: str,
    expected: object,
) -> None:
    result = interpolate_payload(
        {"value": template},
        _VARS,
        node_outputs={"a|b": {"loaded_rows": 7}},
    )

    assert result == {"value": expected}


def test_node_output_missing_or_container_value_rejected_path_only() -> None:
    with pytest.raises(ParamInterpolationError, match="unresolved_node_output"):
        interpolate_payload(
            {"value": "${nodes.n1.loaded_rows}"},
            _VARS,
            node_outputs={"n1": {}},
        )
    with pytest.raises(ParamInterpolationError, match="unsafe_node_output_value"):
        interpolate_payload(
            {"value": "${nodes.n1.loaded_rows}"},
            _VARS,
            node_outputs={"n1": {"loaded_rows": ["secret-row"]}},
        )


def test_arbitrary_scalar_node_output_is_rejected_at_runtime_boundary() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload(
            {"value": "${nodes.query.password}"},
            _VARS,
            node_outputs={"query": {"password": "sensitive-value"}},
        )

    message = str(exc_info.value)
    assert "forbidden_node_output_field" in message
    assert "sensitive-value" not in message


def test_invalid_node_output_reference_uses_stable_code_without_raw_expression() -> None:
    raw_expression = "nodes.query.bad-field"
    payload_secret = "sensitive-payload-value"

    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload(
            {
                "value": f"${{{raw_expression}}}",
                "metadata": payload_secret,
            },
            _VARS,
            node_outputs={"query": {}},
        )

    message = str(exc_info.value)
    assert message == "invalid_node_output_reference"
    assert raw_expression not in message
    assert payload_secret not in message


def test_dotted_reference_unsupported() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${foo.bar}"}, _VARS)
    assert "unsupported_param_reference" in str(exc_info.value)


def test_unknown_filter_rejected() -> None:
    # sql_in / csv 之外的过滤器仍显式报错(消息带原始表达式,无取值)
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${today | upper}"}, _VARS)
    assert "unsupported_param_filter" in str(exc_info.value)
    assert "today | upper" in str(exc_info.value)


# ── C-7 PR3:list 变量 + sql_in / csv 过滤器 ─────────────────────────────────

_LIST_VARS: dict[str, str | list[str]] = {
    "ids": ["1", "2", "3"],
    "codes": ["a", "b"],
    "regions": ["ap-east-1", "us-west-2"],
    "empty": [],
    "single": "solo",
    "today": "2026-07-07",
}


def test_sql_in_numbers_rendered_bare() -> None:
    result = interpolate_payload({"sql": "WHERE id IN (${ids | sql_in})"}, _LIST_VARS)
    assert result["sql"] == "WHERE id IN (1, 2, 3)"


def test_sql_in_strings_single_quoted() -> None:
    result = interpolate_payload({"sql": "WHERE code IN (${codes | sql_in})"}, _LIST_VARS)
    assert result["sql"] == "WHERE code IN ('a', 'b')"


def test_sql_in_mixed_and_hyphenated_strings_quoted() -> None:
    # 含 `-` 的值不是纯数字形态 → 加引号
    result = interpolate_payload({"sql": "${regions | sql_in}"}, _LIST_VARS)
    assert result["sql"] == "'ap-east-1', 'us-west-2'"


def test_sql_in_empty_list_renders_null_not_empty_parens() -> None:
    # ★ 空 list → NULL,避免 `IN ()` 语法错
    result = interpolate_payload({"sql": "WHERE id IN (${empty | sql_in})"}, _LIST_VARS)
    assert result["sql"] == "WHERE id IN (NULL)"


def test_sql_in_single_scalar_treated_as_one_element_list() -> None:
    # 单标量 str → 单元素 list;非数字形态 → 加引号
    result = interpolate_payload({"sql": "${single | sql_in}"}, _LIST_VARS)
    assert result["sql"] == "'solo'"
    # 内置日期(受控格式,非纯数字)→ 加引号
    result2 = interpolate_payload({"sql": "d IN (${today | sql_in})"}, _LIST_VARS)
    assert result2["sql"] == "d IN ('2026-07-07')"


def test_sql_in_quote_escaping_is_defense_in_depth() -> None:
    # 入口 charset 已禁引号,故正常 list 元素不含 `'`;此处直接喂含引号值到纯函数层,
    # 证明 `'`→`''` 转义作为纵深防御生效(不会逃逸出字符串字面量)
    variables: dict[str, str | list[str]] = {"names": ["o'brien", "smith"]}
    result = interpolate_payload({"sql": "${names | sql_in}"}, variables)
    assert result["sql"] == "'o''brien', 'smith'"


def test_csv_joins_without_quotes() -> None:
    result = interpolate_payload({"sql": "${codes | csv}"}, _LIST_VARS)
    assert result["sql"] == "a,b"
    result2 = interpolate_payload({"ids": "${ids | csv}"}, _LIST_VARS)
    assert result2["ids"] == "1,2,3"


def test_bare_list_variable_without_filter_rejected() -> None:
    # ★ 裸 ${listvar}(无过滤器)→ 报错:list 必须经 sql_in / csv 消费,
    # 绝不被误 stringify 成 "['1', '2', '3']" 拼进 SQL
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${ids}"}, _LIST_VARS)
    message = str(exc_info.value)
    assert "list_variable_requires_filter" in message
    assert "ids" in message


def test_filter_on_missing_variable_still_name_only() -> None:
    # 过滤器分支同样严格缺失,且错误只含变量名(R5)
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload({"sql": "${nope | sql_in}"}, _LIST_VARS)
    assert "unresolved_param_variable" in str(exc_info.value)
    assert "nope" in str(exc_info.value)


def test_filter_on_node_reference_rejected() -> None:
    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload(
            {"sql": "${nodes.n1.loaded_rows | sql_in}"},
            _LIST_VARS,
            node_outputs={"n1": {"loaded_rows": 3}},
        )
    assert "unsupported_node_output_filter" in str(exc_info.value)


def test_node_output_filter_error_is_code_only_without_payload_echo() -> None:
    raw_expression = "nodes.n1.loaded_rows | sensitive-filter-sentinel"
    payload_sentinel = "sensitive-payload-sentinel"

    with pytest.raises(ParamInterpolationError) as exc_info:
        interpolate_payload(
            {
                "value": f"${{{raw_expression}}}",
                "metadata": payload_sentinel,
            },
            _LIST_VARS,
            node_outputs={"n1": {"loaded_rows": 3}},
        )

    message = str(exc_info.value)
    assert message == "unsupported_node_output_filter"
    assert raw_expression not in message
    assert "sensitive-filter-sentinel" not in message
    assert payload_sentinel not in message


def test_custom_variable_resolves_in_compare_table_name() -> None:
    # C-7 PR2 闭环:触发/spec 自定义变量进快照后,插值把 ${myvar} 解析进 compare 节点
    # 的 table_name(值已在入口过安全字符集校验,插值层无需再判别)
    variables = {"myvar": "2026Q3", "today": "2026-07-07"}
    payload = {"source_ref": {"table_name": "sales_${myvar}"}}
    result = interpolate_payload(payload, variables)
    assert result["source_ref"]["table_name"] == "sales_2026Q3"


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
