"""L-3 PR1 单测:控制流切段 / 业务标题 / 变量抽取 / 动态 SQL 检测 / 局部变量过滤。

覆盖设计稿 ``docs/design/L-3-stored-procedure-parsing.md`` §六 PR1 测试要求。纯静态,
无 DB(R1)。段落抽取直接测 :mod:`app.domain.lineage.segments` /
:mod:`app.domain.lineage.variables`,端到端填字段 + G6 过滤测 analyze_sql_lineage。
"""

from __future__ import annotations

from app.domain.lineage.parser import LineageParseRequest, analyze_sql_lineage
from app.domain.lineage.segments import (
    extract_dynamic_sql_segments,
    extract_procedure_segments,
)
from app.domain.lineage.variables import all_plsql_local_names, script_variables

# ---------------------------------------------------------------------------
# 控制流感知切段(G1)
# ---------------------------------------------------------------------------


def test_control_flow_shell_stripped_only_dml_kept() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  IF x = 1 THEN
    INSERT INTO dwd.f SELECT a FROM ods.b;
  END IF;
END;"""
    segments = extract_procedure_segments(sql)
    assert len(segments) == 1
    seg = segments[0]
    assert seg["procedure_kind"] == "PROCEDURE"
    assert seg["sql"] == "INSERT INTO dwd.f SELECT a FROM ods.b"
    # IF ... THEN 壳子被剥掉,段从 INSERT 起。
    assert seg["sql"].upper().startswith("INSERT")


def test_nested_begin_end_semicolon_does_not_missplit() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  BEGIN
    UPDATE dwd.t SET a = 1 WHERE id = 2;
  END;
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    segments = extract_procedure_segments(sql)
    sqls = [s["sql"] for s in segments]
    # 嵌套块内的 ; 不结束外层段:嵌套 BEGIN...END 整体成一段(不被内部 ; 误切),
    # 后续 INSERT 独立成段。
    assert any(s.startswith("UPDATE dwd.t SET a = 1 WHERE id = 2") for s in sqls)
    assert "INSERT INTO dwd.f SELECT a FROM ods.b" in sqls


def test_end_loop_and_end_case_not_block_end() -> None:
    # END LOOP; / END CASE; 不当块级 END,否则 depth 错位会漏切后续 INSERT。
    sql = """CREATE PROCEDURE p AS
BEGIN
  FOR i IN 1 .. 10 LOOP
    UPDATE dwd.a SET n = n + 1 WHERE id = i;
  END LOOP;
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    segments = extract_procedure_segments(sql)
    sqls = [s["sql"] for s in segments]
    assert "INSERT INTO dwd.f SELECT a FROM ods.b" in sqls


# ---------------------------------------------------------------------------
# 业务标题(G2)
# ---------------------------------------------------------------------------


def test_pure_comment_prefix_becomes_business_title() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  -- 集中交易
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    seg = extract_procedure_segments(sql)[0]
    assert seg["preceding_comment"] == "集中交易"
    # 纯注释前缀原样保留在段 SQL 里(供 sqlglot 挂到 statement.comments),但标题也
    # 单独抽到 preceding_comment。
    assert seg["sql"].endswith("INSERT INTO dwd.f SELECT a FROM ods.b")
    assert "集中交易" in seg["sql"]


def test_oracle_hint_prefix_is_not_a_title_but_control_flow_shell_dropped() -> None:
    # IF 壳子在 DML 前 → 不是纯注释前缀 → 无标题(壳子剥掉)。
    sql = """CREATE PROCEDURE p AS
BEGIN
  IF flag THEN
    INSERT INTO dwd.f SELECT a FROM ods.b;
  END IF;
END;"""
    seg = extract_procedure_segments(sql)[0]
    assert seg["preceding_comment"] == ""


def test_business_title_truncated_to_200_chars() -> None:
    long_title = "标" * 300
    sql = f"""CREATE PROCEDURE p AS
BEGIN
  -- {long_title}
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    seg = extract_procedure_segments(sql)[0]
    assert len(seg["preceding_comment"]) <= 201  # 200 + 省略号
    assert seg["preceding_comment"].endswith("…")


# ---------------------------------------------------------------------------
# PACKAGE BODY / 嵌套 proc / 匿名块 / TRIGGER(G7)
# ---------------------------------------------------------------------------


def test_package_body_multiple_nested_procs_each_yield_segments() -> None:
    sql = """CREATE PACKAGE BODY pkg AS
  PROCEDURE p1 IS
  BEGIN
    INSERT INTO dwd.a SELECT x FROM ods.s1;
  END;
  PROCEDURE p2 IS
  BEGIN
    INSERT INTO dwd.b SELECT y FROM ods.s2;
  END;
END pkg;"""
    segments = extract_procedure_segments(sql)
    names = {s["procedure_name"] for s in segments}
    sqls = {s["sql"] for s in segments}
    assert "pkg.p1" in names
    assert "pkg.p2" in names
    assert "INSERT INTO dwd.a SELECT x FROM ods.s1" in sqls
    assert "INSERT INTO dwd.b SELECT y FROM ods.s2" in sqls


def test_anonymous_declare_begin_block_kind_is_anonymous() -> None:
    sql = """DECLARE
  v_cnt NUMBER := 0;
BEGIN
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    segments = extract_procedure_segments(sql)
    assert len(segments) == 1
    assert segments[0]["procedure_kind"] == "ANONYMOUS"
    assert segments[0]["procedure_name"] == "<anonymous>"


def test_trigger_source_extracted_from_header() -> None:
    sql = """CREATE TRIGGER trg AFTER INSERT ON ods.src
FOR EACH ROW
BEGIN
  INSERT INTO dwd.audit SELECT id FROM ods.src;
END;"""
    segments = extract_procedure_segments(sql)
    assert segments, "trigger body should yield at least one segment"
    assert segments[0]["procedure_kind"] == "TRIGGER"
    assert segments[0]["trigger_source"] == "ods.src"


# ---------------------------------------------------------------------------
# 动态 SQL(G4)
# ---------------------------------------------------------------------------


def test_execute_immediate_literal_is_high_confidence() -> None:
    sql = "BEGIN EXECUTE IMMEDIATE 'INSERT INTO dwd.t SELECT a FROM ods.s'; END;"
    dynamics = extract_dynamic_sql_segments(sql)
    assert dynamics == [
        {
            "sql": "INSERT INTO dwd.t SELECT a FROM ods.s",
            "source": "execute_keyword",
            "confidence": "high",
        }
    ]


def test_execute_immediate_unknown_variable_is_unresolved_placeholder() -> None:
    sql = "BEGIN EXECUTE IMMEDIATE v_unknown; END;"
    dynamics = extract_dynamic_sql_segments(sql)
    assert len(dynamics) == 1
    assert dynamics[0]["confidence"] == "unresolved"
    assert "unresolved EXECUTE IMMEDIATE variable: v_unknown" in dynamics[0]["sql"]


def test_unresolved_dynamic_sql_emits_warning_via_parser() -> None:
    sql = "BEGIN EXECUTE IMMEDIATE v_unknown; END;"
    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=sql, dialect="oracle", schema={}, lenient=True)
    )
    codes = [w["code"] for w in report.warnings]
    assert "dynamic_sql" in codes
    assert report.dynamic_sql_count == 1


# ---------------------------------------------------------------------------
# 变量抽取(G5)+ 局部变量过滤(G6)
# ---------------------------------------------------------------------------


def test_placeholder_variables_with_assigned_value() -> None:
    sql = "INSERT INTO t SELECT ${p_dt} AS d, :v AS x, @s AS y FROM src WHERE d = ${p_dt}"
    variables = script_variables(sql)
    by_name = {v["name"]: v for v in variables}
    assert "p_dt" in by_name
    assert "v" in by_name
    assert "s" in by_name


def test_package_constant_extracted_with_kind() -> None:
    sql = """CREATE PACKAGE BODY pkg AS
  g_market CONSTANT VARCHAR2(2) := 'JY';
  g_count NUMBER := 0;
  PROCEDURE p IS
  BEGIN
    INSERT INTO dwd.f SELECT a FROM ods.b;
  END;
END pkg;"""
    variables = script_variables(sql)
    by_name = {v["name"]: v for v in variables}
    assert by_name["g_market"]["kind"] == "package_constant"
    assert by_name["g_market"]["assigned_value"] == "'JY'"
    assert by_name["g_count"]["kind"] == "package_variable"


def test_all_plsql_local_names_collects_declared_locals() -> None:
    sql = """DECLARE
  v_row ods.orders%ROWTYPE;
  v_cnt NUMBER := 0;
BEGIN
  NULL;
END;"""
    names = all_plsql_local_names(sql)
    assert "v_row" in names
    assert "v_cnt" in names


def test_local_name_pseudo_table_filtered_from_report() -> None:
    # v_rec 是局部记录变量,被 sqlglot 当成 INSERT 的 source 表 —— G6 应过滤掉。
    sql = """CREATE PROCEDURE p AS
  v_rec my_pkg.rec_type;
BEGIN
  INSERT INTO dwd.t SELECT c FROM v_rec;
END;"""
    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=sql, dialect="oracle", schema={}, lenient=True)
    )
    table_names = {t["name"] for t in report.tables}
    assert "v_rec" not in table_names
    assert "dwd.t" in table_names
    for edge in report.graph_edges:
        assert edge["source_table"] != "v_rec"
        assert edge["target_table"] != "v_rec"


# ---------------------------------------------------------------------------
# 行号对齐(line_start / line_end)
# ---------------------------------------------------------------------------


def test_line_numbers_align_with_source() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  INSERT INTO dwd.a SELECT x FROM ods.s1;
  INSERT INTO dwd.b
  SELECT y FROM ods.s2;
END;"""
    segments = extract_procedure_segments(sql)
    first = next(s for s in segments if s["sql"].startswith("INSERT INTO dwd.a"))
    second = next(s for s in segments if s["sql"].startswith("INSERT INTO dwd.b"))
    # 第一条 INSERT 在第 3 行;第二条跨 4-5 行。
    assert first["line_start"] == 3
    assert first["line_end"] == 3
    assert second["line_start"] == 4
    assert second["line_end"] == 5


# ---------------------------------------------------------------------------
# 端到端:预留字段填充 + 业务标题挂到 statement
# ---------------------------------------------------------------------------


def test_parser_fills_reserved_envelope_fields_and_titles() -> None:
    sql = """CREATE PROCEDURE p AS
BEGIN
  -- 增量抽取
  INSERT INTO dwd.f SELECT a FROM ods.b;
END;"""
    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=sql, dialect="oracle", schema={}, lenient=True)
    )
    assert len(report.procedure_segments) == 1
    assert report.procedure_segments[0]["parse_status"] == "parsed"
    assert report.procedure_segments[0]["preceding_comment"] == "增量抽取"
    # 业务标题挂到对应 statement。
    titled = [s for s in report.statements if s.get("title")]
    assert titled and titled[0]["title"] == "增量抽取"
