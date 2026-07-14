"""Workflow when 条件求值器单测(PR-4;安全白名单 + 1.x 最小子集语义)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.workflow_when import (
    WhenEvaluationError,
    builtin_when_variables,
    evaluate_when,
    when_variables_from_payload,
)

_NOW = datetime(2026, 7, 2, 8, 30, 0, tzinfo=UTC)
_VARS = builtin_when_variables(_NOW)


def test_empty_or_none_when_means_always_run() -> None:
    assert evaluate_when(None, _VARS) is True
    assert evaluate_when("", _VARS) is True
    assert evaluate_when("   ", _VARS) is True


def test_builtin_variables_are_deterministic_strings() -> None:
    assert _VARS == {
        "today": "2026-07-02",
        "now": "2026-07-02T08:30:00+00:00",
        "year": "2026",
        "month": "07",
        "day": "02",
    }


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("true", True),
        ("false", False),
        ("1 == 1", True),
        ("1 != 1", False),
        ("'a' < 'b'", True),
        ("${day} == '02'", True),
        ("${day} == '01'", False),
        ("${month} == '07' && ${day} == '02'", True),
        ("${month} == '01' || ${day} == '02'", True),
        ("!(${day} == '02')", False),
        ("${today} >= '2026-01-01'", True),
        ("null == null", True),
        ("-1 < 0", True),
        ("1 < 2 < 3", True),
        ("1 < 3 < 2", False),
    ],
)
def test_supported_expression_shapes(expression: str, expected: bool) -> None:
    assert evaluate_when(expression, _VARS) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",  # Call + Attribute
        "().__class__",  # Attribute
        "[1,2][0]",  # List + Subscript
        "x == 1",  # 裸标识符(Name 不在白名单)
        "(lambda: 1)()",  # Lambda + Call
        "1 if true else 2",  # IfExp
        "1 + 1 == 2",  # BinOp 不开放
        "'a' in 'abc'",  # In 比较不开放
    ],
)
def test_forbidden_constructs_rejected(expression: str) -> None:
    with pytest.raises(WhenEvaluationError):
        evaluate_when(expression, _VARS)


def test_unknown_variable_and_unknown_node_output_rejected() -> None:
    with pytest.raises(WhenEvaluationError, match="unknown_when_variable"):
        evaluate_when("${no_such_var} == '1'", _VARS)
    with pytest.raises(WhenEvaluationError, match="unresolved_node_output"):
        evaluate_when(
            "${nodes.n1.loaded_rows} > 0",
            _VARS,
            node_outputs={"n1": {}},
        )


def test_when_compares_scalar_node_outputs_without_string_coercion() -> None:
    outputs: dict[str, dict[str, object]] = {
        "compare": {"diff_count": 2, "status": "success"},
        "lineage": {"cached": False},
    }
    assert evaluate_when(
        "${nodes.compare.diff_count} > 0 && ${nodes.compare.status} == 'success'",
        _VARS,
        node_outputs=outputs,
    )
    assert not evaluate_when(
        "${nodes.lineage.cached} == true",
        _VARS,
        node_outputs=outputs,
    )


def test_syntax_error_rejected() -> None:
    with pytest.raises(WhenEvaluationError, match="invalid_when_syntax"):
        evaluate_when("${day} ==", _VARS)


def test_incomparable_types_rejected_not_silently_false() -> None:
    with pytest.raises(WhenEvaluationError, match="incomparable_when_operands"):
        evaluate_when("${day} < 3", _VARS)  # str vs int


def test_over_length_expression_rejected() -> None:
    with pytest.raises(WhenEvaluationError, match="when_too_long"):
        evaluate_when("true || " * 100 + "true", _VARS)


def test_variable_value_is_quoted_not_injected() -> None:
    # 变量值经 repr 变成字符串字面量:恶意值只是字符串内容,注入不出新语法结构,
    # __import__ 永远不会被求值(白名单也没有 Call/Name)。
    malicious = {"day": "02' == '02') or __import__('os"}
    assert evaluate_when("${day} == ${day}", malicious) is True
    assert evaluate_when("${day} == 'harmless'", malicious) is False


def test_variable_with_quote_is_escaped_to_plain_string() -> None:
    tricky = {"day": "it's"}
    assert evaluate_when('${day} == "it\'s"', tricky) is True


def test_interpolated_value_with_sugar_token_not_rewritten() -> None:
    # 回归(backlog #92 评论 minor #2):糖替换先于插值,变量值里的 `&&` 不会被
    # 当语法糖改写。旧顺序(先插值后糖替换)会把值 "x&&y" 里的 `&&` 重写成 " and ",
    # 错误地等于另一个值 "x and y";新顺序保持值原样,两个不同的值比较得 False。
    variables = {"a": "x&&y", "b": "x and y"}
    assert evaluate_when("${a} == ${b}", variables) is False


def test_interpolated_value_null_stays_string_not_none() -> None:
    # 值 "null" 插入后是字符串字面量 'null',不会被糖替换成 Python None;
    # 而表达式里裸写的 `null` 仍走糖(→ None),两者互不干扰。
    variables = {"a": "null"}
    assert evaluate_when("${a} == ${a}", variables) is True  # 'null' == 'null'
    assert evaluate_when("${a} != null", variables) is True  # 字符串 'null' != None


def test_when_variables_from_payload_reads_frozen_snapshot() -> None:
    payload: dict[str, object] = {"when_variables": {"day": "99", "month": "07"}}
    assert when_variables_from_payload(payload) == {"day": "99", "month": "07"}


def test_when_variables_from_payload_rejects_missing_or_malformed() -> None:
    # 缺失 / 非 dict / 非 str-或-list 值:回退 None(调用方按当前时刻计算)
    assert when_variables_from_payload({}) is None
    assert when_variables_from_payload({"when_variables": "not-a-dict"}) is None
    assert when_variables_from_payload({"when_variables": {"day": 2}}) is None
    # list 里含非 str 元素 → 形状不对 → None
    assert when_variables_from_payload({"when_variables": {"ids": [1, 2]}}) is None


def test_when_variables_from_payload_accepts_str_and_list_snapshot() -> None:
    # C-7 PR3:快照值放宽为 str | list[str](list 供 sql_in / csv 展开)
    payload: dict[str, object] = {
        "when_variables": {"day": "99", "ids": ["1", "2", "3"], "empty": []}
    }
    assert when_variables_from_payload(payload) == {
        "day": "99",
        "ids": ["1", "2", "3"],
        "empty": [],
    }


def test_when_eval_ignores_unreferenced_list_var_in_snapshot() -> None:
    # ★ CARE POINT:list 变量只存在于快照(未被 when 引用)→ 不进入插值,
    # when 只引用 str/内置变量,照常求值,不崩溃
    variables: dict[str, str | list[str]] = {
        **builtin_when_variables(_NOW),
        "env": "prod",
        "ids": ["1", "2", "3"],  # list 变量存在但下面 when 不引用它
    }
    assert evaluate_when("${env} == 'prod' && ${year} == '2026'", variables) is True
    assert evaluate_when("${env} == 'staging'", variables) is False


def test_when_eval_directly_referencing_list_var_is_contained_error() -> None:
    # when 直接引用 list 变量 → 受控 WhenEvaluationError(非未捕获崩溃),
    # 消息只含变量名(R5)
    variables: dict[str, str | list[str]] = {"ids": ["1", "2"]}
    with pytest.raises(WhenEvaluationError) as exc_info:
        evaluate_when("${ids} == '1'", variables)
    assert "list_variable_in_when" in str(exc_info.value)
    assert "ids" in str(exc_info.value)
