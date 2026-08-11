"""L-3 存储过程深度解析 domain 层:PL/SQL 变量抽取 + 局部变量误判过滤。

纯正则 + 字符串扫描,无 IO / 无 DB(R1:domain 层禁 DB 驱动)。移植自 1.x
``app/lineage/variables.py`` 已验证逻辑(设计稿 L-3 §一 A6)。

对外入口:
- :func:`script_variables` —— 抽 ``${name}`` / ``:name`` / ``@name`` 占位符 + PACKAGE
  BODY / DECLARE 节的 CONSTANT / 变量声明,供前端变量面板展示。
- :func:`normalize_template_variables` —— 在交给 sqlglot 前把 ``${name}`` 规范为
  ``:name``,避免被误解为 STRUCT;替换保持文本长度与换行不变,便于错误定位。
- :func:`package_variables` —— 只抽 PACKAGE BODY 顶层与 DECLARE 块的变量/常量声明。
- :func:`all_plsql_local_names` —— 扫所有声明区收集**所有局部变量名**(不进面板),
  专用于过滤 ``SELECT INTO v_row`` 被 sqlglot 重写成 ``CREATE TABLE v_row`` 后
  ``v_row`` 混进 tables 的误判(设计稿 G6)。
"""

from __future__ import annotations

import re

_RE_TEMPLATE_VARIABLE = re.compile(r"\$\{\s*([A-Za-z_][\w$#]*)\s*\}")


def _unique_strings(values: list[str]) -> list[str]:
    """保序去重(大小写敏感,与 1.x ``_common.unique_strings`` 语义一致)。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def normalize_template_variables(sql: str) -> str:
    """把 ``${name}`` 规范为 sqlglot 能稳定识别的命名占位符 ``:name``。

    这里只做静态血缘解析所需的语法规范化,不读取或猜测变量实际值。替换结果用
    尾随空格补齐原占位符长度,因此总字符数与换行位置保持不变;后续 ParseError 的
    line / col 仍可直接对应用户输入的原始 SQL。
    """

    def _replace(match: re.Match[str]) -> str:
        placeholder = f":{match.group(1)}"
        return placeholder.ljust(len(match.group(0)))

    return _RE_TEMPLATE_VARIABLE.sub(_replace, sql)


def script_variables(sql: str) -> list[dict[str, str]]:
    """抽脚本变量:``${name}`` / ``:name`` / ``@name`` 占位符 + 包/DECLARE 声明。

    **必须在 normalize 之前抽** —— normalize 会把 ``${name}`` → ``:name``,之后识别不出
    原始模板名。
    """
    variables: list[dict[str, str]] = []
    seen: set[str] = set()
    for variable in variable_names(sql):
        seen.add(variable.lower())
        variables.append(
            {
                "name": variable,
                "placeholder": variable,
                "assigned_value": assigned_value(sql, variable),
            }
        )
    # 把 PL/SQL PACKAGE BODY / DECLARE 节里的 CONSTANT / 普通变量声明也暴露出来。
    for entry in package_variables(sql):
        if entry["name"].lower() in seen:
            continue
        seen.add(entry["name"].lower())
        variables.append(entry)
    return variables


def variable_names(sql: str) -> list[str]:
    names: list[str] = []
    names.extend(match.group(1) for match in _RE_TEMPLATE_VARIABLE.finditer(sql))
    for pattern in [r"(?<!:):([A-Za-z_][\w$#]*)", r"@([A-Za-z_][\w$#]*)"]:
        names.extend(match.group(1) for match in re.finditer(pattern, sql))
    return _unique_strings(names)


def assigned_value(sql: str, variable: str) -> str:
    escaped = re.escape(variable)
    patterns = [
        rf"\b{escaped}\b\s*:=\s*(.*?)(?:;|\n|$)",
        rf"\b{escaped}\b\s*=\s*(.*?)(?:;|\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, sql, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return " ".join(match.group(1).strip().split())
    return ""


# ---------------------------------------------------------------------------
# PL/SQL PACKAGE BODY / DECLARE 节的变量 / 常量声明(设计稿 A6)
# ---------------------------------------------------------------------------
#
# Oracle PL/SQL 里两种典型形态:
#   PACKAGE BODY pkg_x AS
#     g_const CONSTANT VARCHAR2(32) := 'JY';
#     g_var   NUMBER := 0;
#     PROCEDURE p IS BEGIN ... END;
#   END;
#
#   DECLARE
#     v_cnt NUMBER := 100;
#   BEGIN ... END;
#
# 只抽 PACKAGE BODY 顶层(IS/AS 之后到第一个 PROCEDURE/FUNCTION 之前)和 DECLARE 块。
# 形如 `name [CONSTANT] TYPE [(N[,M])] [:= literal_or_expr];`
_RE_PACKAGE_BODY_HEAD = re.compile(
    r"\bPACKAGE\s+BODY\s+[\w$#.]+\s+(?:IS|AS)\b",
    flags=re.IGNORECASE,
)
_RE_DECLARE_HEAD = re.compile(r"\bDECLARE\b", flags=re.IGNORECASE)
_RE_FIRST_PROC_OR_FUNC = re.compile(
    r"\b(?:PROCEDURE|FUNCTION|BEGIN)\b",
    flags=re.IGNORECASE,
)
# 一行声明:`name [CONSTANT] TYPE := value;`(type 可能含 (n) 或 (n,m),可能引用 %TYPE)
_RE_DECLARATION = re.compile(
    r"^\s*([A-Za-z_][\w$#]*)\s+(CONSTANT\s+)?"
    r"(?:[A-Za-z_][\w$#.]*(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?(?:%TYPE)?)\s*"
    r"(?::=\s*(.+?))?\s*;",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def package_variables(sql: str) -> list[dict[str, str]]:
    """抽 PACKAGE BODY 顶层 / DECLARE 节的变量与常量声明。

    返回列表元素与 :func:`script_variables` 兼容(name / placeholder /
    assigned_value),额外带 ``kind``(package_constant / package_variable /
    declare_constant / declare_variable)方便前端区分展示。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for region_text, kind_prefix in _iter_declaration_regions(sql):
        for m in _RE_DECLARATION.finditer(region_text):
            name = m.group(1)
            is_const = bool(m.group(2))
            value = (m.group(3) or "").strip()
            # 排除 PL/SQL 关键字误识别(CURSOR / TYPE / PROCEDURE / 等)。
            if name.upper() in _DECL_NOISE:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": name,
                    "placeholder": name,
                    "assigned_value": " ".join(value.split()) if value else "",
                    "kind": f"{kind_prefix}_{'constant' if is_const else 'variable'}",
                }
            )
    return out


_DECL_NOISE = {
    "CURSOR",
    "TYPE",
    "SUBTYPE",
    "PROCEDURE",
    "FUNCTION",
    "BEGIN",
    "END",
    "IF",
    "ELSIF",
    "ELSE",
    "FOR",
    "WHILE",
    "LOOP",
    "CASE",
    "WHEN",
    "THEN",
    "RETURN",
    "EXCEPTION",
    "PRAGMA",
}


# 扫所有 PL/SQL 局部变量名(含 PROCEDURE 体内)—— 不进 result.variables 列表(避免
# 污染前端面板),但用于过滤,让 `v_row.id` 这种记录字段引用不被误归到表上。
# 形态:`name [CONSTANT] <type_or_ref>;`,type 可能是基础类型 / `tab%ROWTYPE` /
# `tab.col%TYPE` / 引用其他变量等,不强校验类型,只看 `name TYPE_LIKE_TOKEN ;`
_RE_LOCAL_DECL = re.compile(
    r"\b([A-Za-z_][\w$#]*)\s+"
    r"(?:CONSTANT\s+)?"
    r"[A-Za-z_][\w$#.%]*(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?"
    r"(?:\s*:=\s*[^;]+)?"
    r"\s*;",
    flags=re.IGNORECASE,
)


def all_plsql_local_names(sql: str) -> set[str]:
    """扫整个脚本找所有可能的 PL/SQL 局部变量声明,返回名称集合(小写)。

    包括 PACKAGE BODY 顶层 / DECLARE 块 / PROCEDURE/FUNCTION 体的 IS/AS 段
    (即 BEGIN 之前的声明区)。这个集合给 tables 过滤用 —— 避免 ``v_row.id`` 这种
    记录字段引用被误识别为表 ``v_row`` 的 column,或 ``SELECT INTO v_row`` 被 sqlglot
    改写成 ``CREATE TABLE v_row`` 后 ``v_row`` 混进 tables。
    """
    names: set[str] = set()
    decl_regions: list[str] = []
    # PACKAGE BODY 顶层
    for m in _RE_PACKAGE_BODY_HEAD.finditer(sql):
        end_match = re.search(r"\bBEGIN\b", sql[m.end() :], flags=re.IGNORECASE)
        end = m.end() + end_match.start() if end_match else m.end() + 2000
        decl_regions.append(sql[m.end() : end])
    # PROCEDURE/FUNCTION ... IS/AS ... BEGIN
    proc_pattern = re.compile(
        r"\b(?:PROCEDURE|FUNCTION)\s+[\w$#.]+(?:\s*\([^)]*\))?"
        r"(?:\s+RETURN\s+[\w$#.%]+)?\s+(?:IS|AS)\b",
        flags=re.IGNORECASE,
    )
    for m in proc_pattern.finditer(sql):
        end_match = re.search(r"\bBEGIN\b", sql[m.end() :], flags=re.IGNORECASE)
        end = m.end() + end_match.start() if end_match else m.end() + 2000
        decl_regions.append(sql[m.end() : end])
    # DECLARE 块
    for m in _RE_DECLARE_HEAD.finditer(sql):
        end_match = re.search(r"\bBEGIN\b", sql[m.end() :], flags=re.IGNORECASE)
        end = m.end() + end_match.start() if end_match else m.end() + 2000
        decl_regions.append(sql[m.end() : end])

    for region in decl_regions:
        for m in _RE_LOCAL_DECL.finditer(region):
            name = m.group(1)
            if name.upper() in _DECL_NOISE:
                continue
            names.add(name.lower())
    return names


def _iter_declaration_regions(sql: str) -> list[tuple[str, str]]:
    """切出可能含变量声明的代码区。返回 ``[(region_text, kind_prefix)]``。

    kind_prefix:
      - ``package`` —— PACKAGE BODY 头到第一个 PROCEDURE/FUNCTION/BEGIN
      - ``declare`` —— DECLARE 到 BEGIN
    """
    regions: list[tuple[str, str]] = []
    # PACKAGE BODY 顶层
    for m in _RE_PACKAGE_BODY_HEAD.finditer(sql):
        start = m.end()
        end_match = _RE_FIRST_PROC_OR_FUNC.search(sql, start)
        end = end_match.start() if end_match else len(sql)
        if end > start:
            regions.append((sql[start:end], "package"))
    # DECLARE 块
    for m in _RE_DECLARE_HEAD.finditer(sql):
        start = m.end()
        # DECLARE 后的首个 BEGIN 是边界(不能用 PROCEDURE/FUNCTION,DECLARE 块通常
        # 嵌在匿名块里)。
        end_match = re.search(r"\bBEGIN\b", sql[start:], flags=re.IGNORECASE)
        end = start + end_match.start() if end_match else len(sql)
        if end > start:
            regions.append((sql[start:end], "declare"))
    return regions
