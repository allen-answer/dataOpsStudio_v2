"""Workflow 节点 ``when`` 条件求值器(2.4.0 PR-4;ADR-0009 决策 3)。

1.x 语义(``services/workflow_engine.py``)的**最小确定性子集**移植:

- ``${var}`` 占位符 + JS 风格糖(``&&`` / ``||`` / ``!`` / ``null/true/false``);
- 仅允许字面量、比较、布尔运算(AST 白名单同 1.x:无函数调用 / 属性访问 /
  下标 / 推导式 / 赋值 / 裸标识符);
- 变量只开放内置确定性集合 ``today / now / year / month / day``
  (1.x 的 ``${nodes.<id>.<path>}`` 节点输出引用不移植:2.0 子 job 产物是
  ResultRef 不是自由 dict,首版不开放跨节点取值)。

★ 安全:1.x 白名单校验后仍走 ``eval(compile(...))``;2.0 **禁止 eval/exec**,
改为对白名单 AST 直接递归解释执行,不存在代码执行面。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from datetime import datetime

from app.domain.workflow import MAX_WHEN_LENGTH

__all__ = [
    "WhenEvaluationError",
    "builtin_when_variables",
    "evaluate_when",
]


class WhenEvaluationError(ValueError):
    """``when`` 表达式非法或求值失败(节点按失败处理,不重试)。"""


_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
# ! → not,但不吃 !=(与 1.x 相同的保守替换)
_BANG_RE = re.compile(r"(?<![=!<>])!(?!=)")

# AST 白名单(与 1.x _WHEN_ALLOWED_AST_NODES 一致):
# 无 Call / Attribute / Subscript / Name / comprehension —— 只剩字面量逻辑。
_ALLOWED_AST_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Constant,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.USub,
    ast.UAdd,
)


def builtin_when_variables(now: datetime) -> dict[str, str]:
    """内置确定性变量(与 1.x _default_variables 同集合,全部字符串)。"""
    return {
        "today": now.date().isoformat(),
        "now": now.isoformat(timespec="seconds"),
        "year": str(now.year),
        "month": f"{now.month:02d}",
        "day": f"{now.day:02d}",
    }


def evaluate_when(expression: str | None, variables: Mapping[str, str]) -> bool:
    """求值 ``when`` 表达式;空 / 空白 = True(总是执行)。

    任何非法构造 / 未知变量 / 类型不可比 → :class:`WhenEvaluationError`。
    """
    if expression is None or not expression.strip():
        return True
    if len(expression) > MAX_WHEN_LENGTH:
        raise WhenEvaluationError(f"when_too_long: when 表达式超过 {MAX_WHEN_LENGTH} 字符")
    rendered = _interpolate(expression, variables)
    rendered = rendered.replace("&&", " and ").replace("||", " or ")
    rendered = _BANG_RE.sub(" not ", rendered)
    rendered = re.sub(r"\bnull\b", "None", rendered)
    rendered = re.sub(r"\btrue\b", "True", rendered)
    rendered = re.sub(r"\bfalse\b", "False", rendered)
    # 行首 `!` 替换成 ` not ` 会留出前导空格,eval 模式会当缩进错误,strip 掉
    rendered = rendered.strip()
    try:
        tree = ast.parse(rendered, mode="eval")
    except SyntaxError as exc:
        raise WhenEvaluationError(f"invalid_when_syntax: {exc.msg}") from exc
    for node in ast.walk(tree):
        # 精确类型匹配(不认子类),与 1.x 白名单判定一致
        if type(node) not in _ALLOWED_AST_NODES:
            raise WhenEvaluationError(
                f"forbidden_when_construct: 不允许的构造 {type(node).__name__}"
            )
    return bool(_eval_node(tree.body))


def _interpolate(expression: str, variables: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name.startswith("nodes."):
            raise WhenEvaluationError(
                "unsupported_when_reference: 2.0 首版 when 不支持节点输出引用 ${nodes.*}"
            )
        if name not in variables:
            raise WhenEvaluationError(f"unknown_when_variable: 未知变量 {name!r}")
        # 内置变量全是字符串;repr 产出带引号的字符串字面量,交给受限 AST 解析
        return repr(str(variables[name]))

    return _PLACEHOLDER_RE.sub(replace, expression)


def _eval_node(node: ast.expr) -> object:
    """在白名单 AST 上直接解释执行(不 compile、不 eval)。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BoolOp):
        value: object = isinstance(node.op, ast.And)
        for child in node.values:
            value = _eval_node(child)
            if isinstance(node.op, ast.And) and not value:
                return value
            if isinstance(node.op, ast.Or) and value:
                return value
        return value
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(operand, (int, float)):
            return -operand if isinstance(node.op, ast.USub) else +operand
        raise WhenEvaluationError(
            f"invalid_when_operand: 一元运算 {type(node.op).__name__} 的操作数非法"
        )
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    raise WhenEvaluationError(f"forbidden_when_construct: 不允许的构造 {type(node).__name__}")


def _compare(op: ast.cmpop, left: object, right: object) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return bool(left == right)
        if isinstance(op, ast.NotEq):
            return bool(left != right)
        if isinstance(op, ast.Lt):
            return bool(left < right)  # type: ignore[operator]
        if isinstance(op, ast.LtE):
            return bool(left <= right)  # type: ignore[operator]
        if isinstance(op, ast.Gt):
            return bool(left > right)  # type: ignore[operator]
        if isinstance(op, ast.GtE):
            return bool(left >= right)  # type: ignore[operator]
    except TypeError as exc:
        raise WhenEvaluationError(f"incomparable_when_operands: {exc}") from exc
    raise WhenEvaluationError(f"forbidden_when_construct: 不允许的比较运算 {type(op).__name__}")
