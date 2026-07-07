"""L-3 PR2 存储过程补链边:游标 / UDF / BULK COLLECT / TRIGGER 启发式表级边。

纯正则 + 字符串扫描,消费 PR1 :func:`segments.extract_procedure_segments` 已抽好的
段元数据(``cursor_sources`` / ``trigger_source`` / ``procedure_kind`` / ``sql``),
无 IO / 无 DB(R1:domain 层禁 DB 驱动)。移植自 1.x ``app/lineage/analyzer.py``
四个 supplemental 函数(设计稿 L-3 §一 A4/A7):

- 游标补链(A4):``FOR rec IN (SELECT ... FROM t) LOOP INSERT INTO tgt VALUES(rec.c)``
  的 VALUES 不是 SELECT,sqlglot 看不到 ``t → tgt`` 边;据 ``cursor_sources`` 叉乘 DML
  target 补 ``CURSOR_LOOP_INSERT`` 边。内联 / 显式声明回填 / LOOP 范围继承三种场景
  的 cursor_sources 都由 PR1 抽好,本模块只据其建边。
- UDF 补链(A7):FUNCTION 体内 ``SELECT ... FROM src``,而 ``INSERT ... VALUES(fn())``
  自身无 source → 补 ``UDF_CALL`` 边。
- BULK COLLECT 补链(A7):``SELECT ... BULK COLLECT INTO v FROM src`` +
  ``FORALL ... INSERT INTO tgt VALUES(v(i).c)`` → 补 ``BULK_COLLECT`` 边。
- TRIGGER 补链(A7):头部 ``AFTER ... ON src``(PR1 抽成 ``trigger_source``)连体内
  DML target → 补 ``TRIGGER`` 边。

补链边全部 ``confidence="medium"`` —— 与 sqlglot 直解的确定性边区分。补错不污染确定性
边:建边前与既有边按 ``(source_table, target_table)`` 去重,确定性边优先(补链不覆盖)。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from app.domain.lineage.models import TableLineageEdge
from app.domain.lineage.segments import _extract_cursor_select_tables

# 补链边 edge_type 常量(与 1.x analyzer 命名一致)。
EDGE_TYPE_CURSOR = "CURSOR_LOOP_INSERT"
EDGE_TYPE_UDF = "UDF_CALL"
EDGE_TYPE_BULK = "BULK_COLLECT"
EDGE_TYPE_TRIGGER = "TRIGGER"
_CONFIDENCE_MEDIUM = "medium"

# 段落首个 DML 语句的 target 表。取第一处匹配即语句主 target;MERGE 的
# ``WHEN MATCHED THEN UPDATE SET`` 里的 SET 靠黑名单排除(避免把 SET 当 target)。
_RE_DML_TARGET = re.compile(
    r"\b(?:INSERT\s+INTO|INSERT|REPLACE\s+INTO|MERGE\s+INTO|UPDATE|"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+TEMPORARY\s+|TEMPORARY\s+|TEMP\s+)?TABLE)\s+"
    r"(?P<target>[\w$#.\"`\[\]]+)",
    flags=re.IGNORECASE,
)
# 捕获到的 target 若是这些关键字,说明匹配落到了子句里(非真实表名),丢弃。
_DML_TARGET_KEYWORD_BLACKLIST = frozenset(
    {"SET", "SELECT", "VALUES", "WHERE", "FROM", "WHEN", "THEN", "USING", "ON"}
)
# 标识符后紧跟 ``(`` —— 函数调用 ``pkg.fn(...)`` 或数组下标 ``v(i)``,两者语法同形。
_RE_CALL_LIKE = re.compile(r"([\w$#.]+)\s*\(", flags=re.IGNORECASE)
# BULK COLLECT INTO <array_var>。
_RE_BULK_COLLECT = re.compile(r"\bBULK\s+COLLECT\s+INTO\s+([\w$#]+)", flags=re.IGNORECASE)


def _normalize_table(name: str) -> str:
    return name.strip().strip('"`[]')


def _extract_dml_target(sql: str) -> str | None:
    """抽段落 DML 的 target 表名(裸名,不补 default_schema —— 与 cursor_sources 同源)。

    DELETE 无「写入源」语义,不产补链 target,返回 None。
    """
    match = _RE_DML_TARGET.search(sql)
    if not match:
        return None
    target = _normalize_table(match.group("target"))
    if not target or target.upper() in _DML_TARGET_KEYWORD_BLACKLIST:
        return None
    return target


def _call_like_names(sql: str) -> set[str]:
    """段落里所有 ``name(`` 形态的标识符(小写),含限定名与其裸末段。

    用于识别 UDF 调用(``pkg.fn()``)与 BULK COLLECT 数组下标(``v(i)``)。
    """
    names: set[str] = set()
    for match in _RE_CALL_LIKE.finditer(sql):
        raw = match.group(1).lower()
        names.add(raw)
        names.add(raw.split(".")[-1])
    return names


def build_supplemental_edges(
    procedure_segments: Iterable[dict[str, Any]],
    existing_edges: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """据过程段元数据建 medium 置信补链边,与既有确定性边去重后返回新边 dict 列表。

    去重键 = ``(source_table, target_table)``:既有确定性边优先,补链边不覆盖;补链
    边内部也按此键去重(同一 source→target 只补一条)。
    """
    segments = list(procedure_segments)
    seen: set[tuple[str, str]] = {
        (str(e.get("source_table") or ""), str(e.get("target_table") or "")) for e in existing_edges
    }
    edges: list[dict[str, Any]] = []

    def emit(source: str, target: str, edge_type: str, reason: str) -> None:
        source = _normalize_table(source)
        target = _normalize_table(target)
        if not source or not target or source == target:
            return
        key = (source, target)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            TableLineageEdge(
                source_table=source,
                target_table=target,
                edge_type=edge_type,
                confidence=_CONFIDENCE_MEDIUM,
                reason=reason,
            ).model_dump(mode="json")
        )

    _cursor_supplemental_edges(segments, emit)
    _trigger_supplemental_edges(segments, emit)
    _udf_supplemental_edges(segments, emit)
    _bulk_collect_supplemental_edges(segments, emit)
    return edges


_Emit = Callable[[str, str, str, str], None]


def _cursor_supplemental_edges(segments: list[dict[str, Any]], emit: _Emit) -> None:
    """游标 FOR loop 源表 → 段 DML target(cursor_sources 由 PR1 三种场景抽好)。"""
    for segment in segments:
        sources = segment.get("cursor_sources") or []
        if not sources:
            continue
        target = _extract_dml_target(str(segment.get("sql") or ""))
        if not target:
            continue
        for source in sources:
            emit(str(source), target, EDGE_TYPE_CURSOR, "cursor FOR loop source table")


def _trigger_supplemental_edges(segments: list[dict[str, Any]], emit: _Emit) -> None:
    """TRIGGER 头部触发源表(PR1 抽的 trigger_source) → 触发体内 DML target。"""
    for segment in segments:
        trigger_source = str(segment.get("trigger_source") or "")
        if not trigger_source:
            continue
        target = _extract_dml_target(str(segment.get("sql") or ""))
        if not target:
            continue
        emit(trigger_source, target, EDGE_TYPE_TRIGGER, "trigger source table")


def _udf_supplemental_edges(segments: list[dict[str, Any]], emit: _Emit) -> None:
    """FUNCTION 体内 SELECT 源表 → 调用该函数的 DML 段 target。

    先建 ``{函数名: [源表]}``(限定名与裸名都登记),再扫非 FUNCTION 段:段内出现
    已知函数调用且段自身是 ``INSERT ... VALUES(fn())`` 之类无 SELECT 源时,把函数源表
    连到段 target。
    """
    fn_sources: dict[str, list[str]] = {}
    for segment in segments:
        if str(segment.get("procedure_kind") or "").upper() != "FUNCTION":
            continue
        name = str(segment.get("procedure_name") or "").strip()
        if not name:
            continue
        sources = _extract_cursor_select_tables(str(segment.get("sql") or ""))
        if not sources:
            continue
        for alias in {name.lower(), name.split(".")[-1].lower()}:
            fn_sources.setdefault(alias, []).extend(sources)
    if not fn_sources:
        return
    for segment in segments:
        if str(segment.get("procedure_kind") or "").upper() == "FUNCTION":
            continue
        sql = str(segment.get("sql") or "")
        target = _extract_dml_target(sql)
        if not target:
            continue
        called = _call_like_names(sql)
        for fn_name in called & fn_sources.keys():
            for source in fn_sources[fn_name]:
                emit(source, target, EDGE_TYPE_UDF, f"UDF {fn_name} source table")


def _bulk_collect_supplemental_edges(segments: list[dict[str, Any]], emit: _Emit) -> None:
    """``SELECT ... BULK COLLECT INTO v FROM src`` 的源表 → 引用数组 v 的 DML 段 target。

    先建 ``{数组变量: [源表]}``,再扫非 BULK COLLECT 段:段内以 ``v(i)`` 形态引用已知
    数组变量时(FORALL ... INSERT),把源表连到段 target。
    """
    var_sources: dict[str, list[str]] = {}
    for segment in segments:
        sql = str(segment.get("sql") or "")
        match = _RE_BULK_COLLECT.search(sql)
        if not match:
            continue
        sources = _extract_cursor_select_tables(sql)
        if not sources:
            continue
        var_sources.setdefault(match.group(1).lower(), []).extend(sources)
    if not var_sources:
        return
    for segment in segments:
        sql = str(segment.get("sql") or "")
        if _RE_BULK_COLLECT.search(sql):
            continue  # BULK COLLECT 语句自身不是消费方
        target = _extract_dml_target(sql)
        if not target:
            continue
        referenced = _call_like_names(sql)
        for var_name in referenced & var_sources.keys():
            for source in var_sources[var_name]:
                emit(source, target, EDGE_TYPE_BULK, f"BULK COLLECT array {var_name} source table")
