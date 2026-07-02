"""Workflow when 条件求值器单测(PR-4;安全白名单 + 1.x 最小子集语义)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.workflow_when import (
    WhenEvaluationError,
    builtin_when_variables,
    evaluate_when,
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


def test_unknown_variable_and_nodes_reference_rejected() -> None:
    with pytest.raises(WhenEvaluationError, match="unknown_when_variable"):
        evaluate_when("${no_such_var} == '1'", _VARS)
    with pytest.raises(WhenEvaluationError, match="unsupported_when_reference"):
        evaluate_when("${nodes.n1.rows} > 0", _VARS)


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
