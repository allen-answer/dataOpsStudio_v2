"""Workflow 节点 ``when`` 条件求值器(2.4.0 PR-4;ADR-0009 决策 3)。

1.x 语义(``services/workflow_engine.py``)的**最小确定性子集**移植:

- ``${var}`` 占位符 + JS 风格糖(``&&`` / ``||`` / ``!`` / ``null/true/false``);
- 仅允许字面量、比较、布尔运算(AST 白名单同 1.x:无函数调用 / 属性访问 /
  下标 / 推导式 / 赋值 / 裸标识符);
- 变量开放冻结快照;2.4.x 另允许调用方传入的
  ``${nodes.<id>.<whitelisted-field>}`` 标量输出,不读取自由 ResultRef。

★ 安全:1.x 白名单校验后仍走 ``eval(compile(...))``;2.0 **禁止 eval/exec**,
改为对白名单 AST 直接递归解释执行,不存在代码执行面。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from datetime import datetime

from app.domain.workflow import MAX_WHEN_LENGTH
from app.domain.workflow_outputs import (
    NodeOutputMap,
    NodeOutputReferenceError,
    parse_node_output_reference,
    resolve_node_output,
)

__all__ = [
    "WhenEvaluationError",
    "builtin_when_variables",
    "evaluate_when",
    "when_variables_from_payload",
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


def _is_valid_snapshot_value(value: object) -> bool:
    """快照值形状:``str`` 或 ``list[str]``(C-7 PR3);其余类型无效。"""
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(isinstance(element, str) for element in value)
    return False


def when_variables_from_payload(
    payload: Mapping[str, object],
) -> dict[str, str | list[str]] | None:
    """读取 run payload 里触发时冻结的 when 变量快照。

    触发端点把合并变量(内置 + spec + 触发)写进 run payload,执行器每步推进与
    API 状态查询都用同一份快照 —— when 决策 + ``${var}`` 插值在 run 生命周期内
    确定不变(跨午夜不翻转),两端口径也不漂移。

    值形状为 ``str`` 或 ``list[str]``(list 供 ``${var | sql_in}`` / ``${var | csv}``
    展开;C-7 PR3)。快照缺失 / 形状不对(在途旧 run / payload 损坏)返回 None,
    调用方回退为按当前时刻计算。
    """
    raw = payload.get("when_variables")
    if not isinstance(raw, dict):
        return None
    if not all(
        isinstance(key, str) and _is_valid_snapshot_value(value) for key, value in raw.items()
    ):
        return None
    return dict(raw)


def evaluate_when(
    expression: str | None,
    variables: Mapping[str, str | list[str]],
    *,
    node_outputs: NodeOutputMap | None = None,
) -> bool:
    """求值 ``when`` 表达式;空 / 空白 = True(总是执行)。

    任何非法构造 / 未知变量 / 类型不可比 → :class:`WhenEvaluationError`。
    """
    if expression is None or not expression.strip():
        return True
    if len(expression) > MAX_WHEN_LENGTH:
        raise WhenEvaluationError(f"when_too_long: when 表达式超过 {MAX_WHEN_LENGTH} 字符")
    # 糖替换在插值**之前**做:模板占位符(${var} + 内置变量名)不含 &&/||/!/null
    # 等 token,先糖化再插值绝对安全;而插值后的变量值(repr 出的字符串字面量)
    # 若含这些 token,此时已不会被改写 —— 避免变量集合扩大(如开放 ${nodes.*})
    # 后字符串内容被误当语法糖重写的注入/正确性雷。
    sugared = expression.replace("&&", " and ").replace("||", " or ")
    sugared = _BANG_RE.sub(" not ", sugared)
    sugared = re.sub(r"\bnull\b", "None", sugared)
    sugared = re.sub(r"\btrue\b", "True", sugared)
    sugared = re.sub(r"\bfalse\b", "False", sugared)
    rendered = _interpolate(sugared, variables, node_outputs or {})
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


def _interpolate(
    expression: str,
    variables: Mapping[str, str | list[str]],
    node_outputs: NodeOutputMap,
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if parse_node_output_reference(name) is not None:
            try:
                return repr(resolve_node_output(name, node_outputs))
            except NodeOutputReferenceError as exc:
                raise WhenEvaluationError(str(exc)) from exc
        if name.startswith("nodes."):
            raise WhenEvaluationError("invalid_node_output_reference")
        if name not in variables:
            raise WhenEvaluationError(f"unknown_when_variable: 未知变量 {name!r}")
        value = variables[name]
        # list 型变量(为 ${var | sql_in} / csv 而生)在 when 里没有明确布尔语义:
        # 显式报错(受控、只含变量名,R5),而非把 "['a','b']" 塞进受限 AST 误比较。
        # ★ 仅当 when 表达式**直接引用**该 list 变量时才触发;list 变量只是存在于
        # 快照里(未被引用)不进入本函数,不影响其它节点的 when 求值。
        if isinstance(value, list):
            raise WhenEvaluationError(f"list_variable_in_when: when 不支持 list 变量 {name!r}")
        # 标量变量全是字符串;repr 产出带引号的字符串字面量,交给受限 AST 解析
        return repr(str(value))

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
