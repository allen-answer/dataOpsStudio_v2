"""L-3 PR2 单测:游标 / UDF / BULK COLLECT / TRIGGER 补链边(confidence=medium)。

覆盖设计稿 ``docs/design/L-3-stored-procedure-parsing.md`` §六 PR2 测试要求。纯静态,
无 DB(R1)。端到端经 :func:`analyze_sql_lineage`(lenient 模式,无元数据缓存),补链
消费 PR1 ``extract_procedure_segments`` 抽好的 ``cursor_sources`` / ``trigger_source``。
"""

from __future__ import annotations

from typing import Any

from app.domain.lineage.parser import LineageParseRequest, analyze_sql_lineage
from app.domain.lineage.supplemental_edges import build_supplemental_edges


def _run(sql: str) -> Any:
    return analyze_sql_lineage(
        LineageParseRequest(sql_text=sql, dialect="oracle", schema={}, lenient=True)
    )


def _edge(report: Any, source: str, target: str) -> dict[str, Any] | None:
    for edge in report.graph_edges:
        edge_dict: dict[str, Any] = edge
        if edge_dict["source_table"] == source and edge_dict["target_table"] == target:
            return edge_dict
    return None


# ---------------------------------------------------------------------------
# 游标补链:内联 / 显式声明回填 / LOOP 范围继承(A4 三场景)
# ---------------------------------------------------------------------------


def test_cursor_inline_for_select_supplements_edge() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  FOR rec IN (SELECT id FROM ods.b) LOOP
    INSERT INTO dwd.f VALUES (rec.id);
  END LOOP;
END;"""
    report = _run(sql)
    edge = _edge(report, "ods.b", "dwd.f")
    assert edge is not None
    assert edge["edge_type"] == "CURSOR_LOOP_INSERT"
    assert edge["confidence"] == "medium"


def test_cursor_explicit_declaration_backfilled() -> None:
    # CURSOR c IS SELECT ...; + FOR rec IN c LOOP —— 声明区回填 cursor_sources。
    sql = """CREATE PROCEDURE p AS
  CURSOR c IS SELECT id FROM ods.src;
BEGIN
  FOR rec IN c LOOP
    INSERT INTO dwd.tgt VALUES (rec.id);
  END LOOP;
END;"""
    report = _run(sql)
    edge = _edge(report, "ods.src", "dwd.tgt")
    assert edge is not None
    assert edge["edge_type"] == "CURSOR_LOOP_INSERT"
    assert edge["confidence"] == "medium"


def test_cursor_loop_scope_inherits_to_multiple_segments() -> None:
    # LOOP 体内被 ; 切碎的多个 DML 段统一继承同一份 cursor_sources。
    sql = """CREATE PROCEDURE p AS
BEGIN
  FOR rec IN (SELECT id, nm FROM ods.b) LOOP
    INSERT INTO dwd.f VALUES (rec.id);
    UPDATE dwd.g SET n = rec.nm WHERE id = rec.id;
  END LOOP;
END;"""
    report = _run(sql)
    insert_edge = _edge(report, "ods.b", "dwd.f")
    update_edge = _edge(report, "ods.b", "dwd.g")
    assert insert_edge is not None and insert_edge["edge_type"] == "CURSOR_LOOP_INSERT"
    assert update_edge is not None and update_edge["edge_type"] == "CURSOR_LOOP_INSERT"


# ---------------------------------------------------------------------------
# 补链不误伤确定性边 + 无游标不产多余边
# ---------------------------------------------------------------------------


def test_supplemental_does_not_duplicate_deterministic_edge() -> None:
    # 已有 SELECT-derived 同边(ods.b -> dwd.f)时,游标补链不重复;确定性边优先(无 edge_type)。
    sql = """CREATE PROCEDURE p AS
BEGIN
  INSERT INTO dwd.f SELECT id FROM ods.b;
  FOR rec IN (SELECT id FROM ods.b) LOOP
    INSERT INTO dwd.f VALUES (rec.id);
  END LOOP;
END;"""
    report = _run(sql)
    matches = [
        e
        for e in report.graph_edges
        if e["source_table"] == "ods.b" and e["target_table"] == "dwd.f"
    ]
    assert len(matches) == 1
    # 确定性边胜出,不被补链元数据覆盖。
    assert matches[0]["edge_type"] is None
    assert matches[0]["confidence"] is None


def test_no_cursor_yields_no_supplemental_edges() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    report = _run(sql)
    assert all(e.get("edge_type") is None for e in report.graph_edges)


def test_plain_dml_script_unaffected() -> None:
    report = _run("INSERT INTO dwd.f SELECT a FROM ods.b")
    assert [(e["source_table"], e["target_table"]) for e in report.graph_edges] == [
        ("ods.b", "dwd.f")
    ]
    assert all(e.get("edge_type") is None for e in report.graph_edges)


# ---------------------------------------------------------------------------
# 次版启发式:UDF / BULK COLLECT / TRIGGER 各一最小用例
# ---------------------------------------------------------------------------


def test_udf_call_supplements_function_source_to_insert_target() -> None:
    sql = """CREATE FUNCTION pkg.getval RETURN NUMBER IS
  v NUMBER;
BEGIN
  SELECT amt INTO v FROM ods.txn;
  RETURN v;
END;
CREATE PROCEDURE p AS
BEGIN
  INSERT INTO dwd.t (c) VALUES (pkg.getval());
END;"""
    report = _run(sql)
    edge = _edge(report, "ods.txn", "dwd.t")
    assert edge is not None
    assert edge["edge_type"] == "UDF_CALL"
    assert edge["confidence"] == "medium"


def test_bulk_collect_supplements_array_source_to_forall_target() -> None:
    sql = """CREATE PROCEDURE p AS
  v tab;
BEGIN
  SELECT c BULK COLLECT INTO v FROM ods.a;
  FORALL i IN 1 .. v.COUNT
    INSERT INTO dwd.b (c) VALUES (v(i).c);
END;"""
    report = _run(sql)
    edge = _edge(report, "ods.a", "dwd.b")
    assert edge is not None
    assert edge["edge_type"] == "BULK_COLLECT"
    assert edge["confidence"] == "medium"


def test_trigger_supplements_source_to_body_target() -> None:
    sql = """CREATE TRIGGER trg AFTER INSERT ON ods.src
FOR EACH ROW
BEGIN
  INSERT INTO dwd.audit (id) VALUES (:new.id);
END;"""
    report = _run(sql)
    edge = _edge(report, "ods.src", "dwd.audit")
    assert edge is not None
    assert edge["edge_type"] == "TRIGGER"
    assert edge["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 直接单元:build_supplemental_edges 去重与自约束
# ---------------------------------------------------------------------------


def test_build_supplemental_edges_dedups_against_existing() -> None:
    segments = [
        {
            "procedure_kind": "PROCEDURE",
            "procedure_name": "p",
            "sql": "INSERT INTO dwd.f VALUES (rec.id)",
            "cursor_sources": ["ods.b"],
            "trigger_source": "",
        }
    ]
    existing = [{"source_table": "ods.b", "target_table": "dwd.f"}]
    assert build_supplemental_edges(segments, existing) == []


def test_build_supplemental_edges_dedups_within_supplemental() -> None:
    # 两个 LOOP 段继承同一 cursor 源、写同一 target,只出一条补链边。
    segments = [
        {
            "procedure_kind": "PROCEDURE",
            "procedure_name": "p",
            "sql": "INSERT INTO dwd.f VALUES (rec.id)",
            "cursor_sources": ["ods.b"],
            "trigger_source": "",
        },
        {
            "procedure_kind": "PROCEDURE",
            "procedure_name": "p",
            "sql": "INSERT INTO dwd.f VALUES (rec.nm)",
            "cursor_sources": ["ods.b"],
            "trigger_source": "",
        },
    ]
    edges = build_supplemental_edges(segments, [])
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "CURSOR_LOOP_INSERT"
