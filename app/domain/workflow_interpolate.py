"""Workflow 节点 payload ``${var}`` 插值引擎(C-7 PR1)。

节点 payload 里的字符串支持 ``${var}`` 占位符,在**子 job 入队时刻**用触发时
冻结的变量快照(与 ``when`` 同源:``workflow_when.builtin_when_variables``)渲染。
本模块只做**纯字符串标量替换**(零 IO、零日志、无 DB —— R1):递归遍历
dict/list/str,把每个 ``${name}`` 替换成 ``variables[name]``,其余值原样透传。

★ PR1 是刻意收窄的安全子集(见设计稿 C-7):

- **只开放内置确定性变量**(``today/now/year/month/day``,格式受控、无引号/分号),
  注入面为零 —— 用户/spec 自定义变量 + 标识符白名单 + SQL 转义留到 PR2;
- **不支持节点输出引用** ``${nodes.*}``(2.0 首版子 job 产物是 ResultRef 不是
  自由 dict,与 ``when`` 口径一致);
- **不支持过滤器** ``${x | sql_in}`` / ``${x | csv}``(留到 PR2),遇到即显式报错,
  不静默放行。

★ R5:任何错误信息**只含变量名 / 占位符表达式,绝不含变量取值**;本模块不打日志。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "ParamInterpolationError",
    "interpolate_payload",
]

# 与 workflow_when._PLACEHOLDER_RE 同形:捕获 ${...} 内的原始表达式
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


class ParamInterpolationError(ValueError):
    """payload 插值失败(未知变量 / 不支持的引用或过滤器)。

    节点按失败处理,不重试(与 ``when`` 求值异常同语义:确定性错误)。
    """


def interpolate_payload(
    payload: Mapping[str, Any],
    variables: Mapping[str, str],
) -> dict[str, Any]:
    """递归渲染 payload 里所有字符串中的 ``${var}`` 占位符。

    - dict / list 递归下钻;str 做标量替换;其余类型(int/float/bool/None 等)原样透传;
    - ``${name}`` 中 ``name`` 不在 ``variables`` → :class:`ParamInterpolationError`
      (消息只含变量名);
    - ``${nodes.*}`` / 含 ``.`` 的引用 → 不支持(PR1 不开放节点输出取值);
    - ``${x | filter}`` → 不支持(过滤器留到 PR2)。

    返回全新 dict(不原地修改入参)。
    """
    rendered: dict[str, Any] = {
        key: _interpolate_value(value, variables) for key, value in payload.items()
    }
    return rendered


def _interpolate_value(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _interpolate_str(value, variables)
    if isinstance(value, Mapping):
        return {key: _interpolate_value(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_value(item, variables) for item in value]
    # 非字符串标量 / 非容器:原样透传(占位符只在字符串里生效)
    return value


def _interpolate_str(text: str, variables: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        # 过滤器(PR2):含 `|` 一律先拦,消息带原始表达式(无取值)
        if "|" in expr:
            raise ParamInterpolationError(f"unsupported_param_filter: {expr}")
        # 节点输出引用:含 `.` 或以 `nodes` 开头,PR1 不开放
        if "." in expr or expr.startswith("nodes"):
            raise ParamInterpolationError(f"unsupported_param_reference: {expr}")
        if expr not in variables:
            # ★ R5:只暴露变量名,绝不含任何取值
            raise ParamInterpolationError(f"unresolved_param_variable: {expr}")
        return str(variables[expr])

    return _PLACEHOLDER_RE.sub(replace, text)
