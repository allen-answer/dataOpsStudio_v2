"""Workflow 节点 payload ``${var}`` 插值引擎(C-7 PR1/PR3)。

节点 payload 里的字符串支持 ``${var}`` 占位符,在**子 job 入队时刻**用触发时
冻结的变量快照(与 ``when`` 同源:``workflow_when``)渲染。本模块只做**纯字符串
替换**(零 IO、零日志、无 DB —— R1):递归遍历 dict/list/str,把每个 ``${name}``
或 ``${name | filter}`` 替换成渲染结果,其余值原样透传。

支持的语法(见设计稿 C-7 §2.1 / §3.1):

- ``${var}`` —— 标量替换(变量必须是 ``str``;list 变量必须走过滤器,见下);
- ``${var | sql_in}`` —— list(或单个标量视作单元素 list)→ SQL ``IN(...)`` 体:
  数字裸出、字符串单引号 + ``'``→``''`` 转义、**空 list → ``NULL``**(避免 ``IN ()``
  语法错);
- ``${var | csv}`` —— 逗号拼接、无引号(数字 ID 用;元素已在入口过安全字符集校验);
- ``${nodes.<id>.<field>}`` —— 仅消费调用方提供的标量白名单输出;完整占位符
  保留类型,嵌入字符串时转字符串;不开放任意 ResultRef metadata;
- **未知过滤器**(除 sql_in / csv 外)→ 显式报错,不静默放行;
- **裸 ``${var}`` 解析到 list → 报错**(``list_variable_requires_filter``):list 只能
  经 sql_in / csv 消费,绝不被误 stringify 成 ``['a','b']`` 拼进 SQL。

★ 注入安全:元素在入口(``validate_workflow_variables``)已过保守安全字符集校验
(排除引号/分号/注释符),``sql_in`` 的 ``'``→``''`` 转义是**纵深防御**(charset
本已禁引号,转义为二次兜底)。故 sql_in/csv 输出按构造即安全。

★ R5:任何错误信息**只含变量名 / 占位符表达式,绝不含变量取值 / list 元素值**;
本模块不打日志。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.workflow_outputs import (
    NodeOutputMap,
    NodeOutputReferenceError,
    parse_node_output_reference,
    resolve_node_output,
)

__all__ = [
    "ParamInterpolationError",
    "interpolate_payload",
]

# 与 workflow_when._PLACEHOLDER_RE 同形:捕获 ${...} 内的原始表达式
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
# 数字形态(sql_in 里裸出、不加引号):整数 / 小数,可带前导负号
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class ParamInterpolationError(ValueError):
    """payload 插值失败(未知变量 / 不支持的引用或过滤器 / list 缺过滤器)。

    节点按失败处理,不重试(与 ``when`` 求值异常同语义:确定性错误)。
    """


def interpolate_payload(
    payload: Mapping[str, Any],
    variables: Mapping[str, str | list[str]],
    *,
    node_outputs: NodeOutputMap | None = None,
) -> dict[str, Any]:
    """递归渲染 payload 里所有字符串中的 ``${var}`` / ``${var | filter}`` 占位符。

    - dict / list 递归下钻;str 做替换;其余类型(int/float/bool/None 等)原样透传;
    - ``${name}`` 中 ``name`` 不在 ``variables`` → :class:`ParamInterpolationError`
      (消息只含变量名);
    - ``${nodes.<id>.<field>}`` 从 ``node_outputs`` 读取受控标量;
    - ``${x | sql_in}`` / ``${x | csv}`` → 过滤器展开;未知过滤器 → 报错;
    - 裸 ``${listvar}``(无过滤器)解析到 list → 报错(list 必须经过滤器消费)。

    返回全新 dict(不原地修改入参)。
    """
    outputs = node_outputs or {}
    rendered: dict[str, Any] = {
        key: _interpolate_value(value, variables, outputs) for key, value in payload.items()
    }
    return rendered


def _interpolate_value(
    value: Any,
    variables: Mapping[str, str | list[str]],
    node_outputs: NodeOutputMap,
) -> Any:
    if isinstance(value, str):
        return _interpolate_str(value, variables, node_outputs)
    if isinstance(value, Mapping):
        return {
            key: _interpolate_value(item, variables, node_outputs) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_interpolate_value(item, variables, node_outputs) for item in value]
    # 非字符串标量 / 非容器:原样透传(占位符只在字符串里生效)
    return value


def _resolve_variable(
    name: str, variables: Mapping[str, str | list[str]], expr: str
) -> str | list[str]:
    """按变量名取值;节点引用 / 缺失变量 → 显式报错(消息不含取值,R5)。"""
    if "." in name or name.startswith("nodes"):
        raise ParamInterpolationError(f"unsupported_param_reference: {expr}")
    if name not in variables:
        # ★ R5:只暴露变量名,绝不含任何取值
        raise ParamInterpolationError(f"unresolved_param_variable: {name}")
    return variables[name]


def _format_sql_in(value: str | list[str]) -> str:
    """渲染 SQL ``IN(...)`` 体:数字裸出、字符串单引号 + ``'``→``''`` 转义。

    单个标量 str 视作单元素 list;**空 list → ``NULL``**(避免 ``IN ()`` 语法错)。
    """
    elements = [value] if isinstance(value, str) else value
    if not elements:
        return "NULL"
    parts: list[str] = []
    for element in elements:
        if _NUMERIC_RE.fullmatch(element):
            parts.append(element)
        else:
            # ``'``→``''`` 转义为纵深防御(入口 charset 已禁引号,此处二次兜底)
            escaped = element.replace("'", "''")
            parts.append(f"'{escaped}'")
    return ", ".join(parts)


def _format_csv(value: str | list[str]) -> str:
    """逗号拼接、无引号(数字 ID 用;元素已在入口过安全字符集校验)。"""
    elements = [value] if isinstance(value, str) else value
    return ",".join(elements)


def _interpolate_str(
    text: str,
    variables: Mapping[str, str | list[str]],
    node_outputs: NodeOutputMap,
) -> Any:
    exact = _PLACEHOLDER_RE.fullmatch(text)
    if exact is not None:
        exact_expr = exact.group(1).strip()
        if parse_node_output_reference(exact_expr) is not None:
            try:
                return resolve_node_output(exact_expr, node_outputs)
            except NodeOutputReferenceError as exc:
                raise ParamInterpolationError(str(exc)) from exc

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if parse_node_output_reference(expr) is not None:
            try:
                return str(resolve_node_output(expr, node_outputs))
            except NodeOutputReferenceError as exc:
                raise ParamInterpolationError(str(exc)) from exc
        if "|" in expr:
            raw_name, _, raw_filter = expr.rpartition("|")
            name = raw_name.strip()
            filter_name = raw_filter.strip()
            if parse_node_output_reference(name) is not None:
                raise ParamInterpolationError("unsupported_node_output_filter")
            if name.startswith("nodes."):
                raise ParamInterpolationError("invalid_node_output_reference")
            value = _resolve_variable(name, variables, expr)
            if filter_name == "sql_in":
                return _format_sql_in(value)
            if filter_name == "csv":
                return _format_csv(value)
            # 未知过滤器:显式报错,消息带原始表达式(无取值)
            raise ParamInterpolationError(f"unsupported_param_filter: {expr}")
        if expr.startswith("nodes."):
            raise ParamInterpolationError("invalid_node_output_reference")
        value = _resolve_variable(expr, variables, expr)
        # ★ 裸 ${listvar}(无过滤器)禁止:list 只能经 sql_in / csv 消费,
        # 绝不被误 stringify 成 "['a','b']" 拼进 SQL(消息只含变量名,R5)
        if isinstance(value, list):
            raise ParamInterpolationError(f"list_variable_requires_filter: {expr}")
        return str(value)

    return _PLACEHOLDER_RE.sub(replace, text)
