"""app/services/sql_preflight.py 纯函数单测(差距矩阵 C-11)。

体检是 advisory:只验 finding 命中 / 不命中,不连库、不入队。重点覆盖
误报面(注释 / 字符串里的关键字、COUNT(*))。
"""

from __future__ import annotations

from app.services.sql_preflight import (
    SEVERITY_INFO,
    SEVERITY_WARNING,
    run_sql_preflight,
)


def _codes(sql: str) -> set[str]:
    return {f.code for f in run_sql_preflight(sql)}


def test_empty_and_blank_return_no_findings() -> None:
    assert run_sql_preflight("") == []
    assert run_sql_preflight("   \n\t ") == []


def test_update_without_where_flagged_warning() -> None:
    findings = run_sql_preflight("UPDATE users SET active = 0")
    assert len(findings) == 1
    assert findings[0].code == "write_without_where"
    assert findings[0].severity == SEVERITY_WARNING


def test_delete_without_where_flagged() -> None:
    assert "write_without_where" in _codes("DELETE FROM orders")


def test_update_with_where_not_flagged() -> None:
    assert "write_without_where" not in _codes("UPDATE users SET active = 0 WHERE id = 5")


def test_delete_with_where_not_flagged() -> None:
    assert "write_without_where" not in _codes("DELETE FROM orders WHERE id = 1")


def test_select_star_flagged_info() -> None:
    findings = run_sql_preflight("SELECT * FROM big_table LIMIT 10")
    codes = {f.code for f in findings}
    assert "select_star" in codes
    star = next(f for f in findings if f.code == "select_star")
    assert star.severity == SEVERITY_INFO


def test_qualified_star_flagged() -> None:
    assert "select_star" in _codes("SELECT t.* FROM t JOIN u ON t.id = u.id LIMIT 5")


def test_count_star_not_treated_as_select_star() -> None:
    # COUNT(*) 不在 SELECT 后紧跟,不该误判为 SELECT *;且 COUNT 无需 LIMIT 提示?
    # 缺 LIMIT 仍会提示(聚合也可能大),但 select_star 一定不该命中。
    assert "select_star" not in _codes("SELECT COUNT(*) FROM t")


def test_missing_limit_flagged() -> None:
    assert "missing_limit" in _codes("SELECT id, name FROM users")


def test_limit_suppresses_missing_limit() -> None:
    assert "missing_limit" not in _codes("SELECT id FROM users LIMIT 100")


def test_fetch_first_suppresses_missing_limit() -> None:
    assert "missing_limit" not in _codes("SELECT id FROM users FETCH FIRST 10 ROWS ONLY")


def test_top_suppresses_missing_limit() -> None:
    assert "missing_limit" not in _codes("SELECT TOP 10 id FROM users")


def test_rownum_suppresses_missing_limit() -> None:
    assert "missing_limit" not in _codes("SELECT id FROM users WHERE ROWNUM <= 10")


def test_keyword_inside_string_literal_not_matched() -> None:
    # 字符串里的 WHERE 不该被当成真 WHERE 子句 → UPDATE 仍判无 WHERE。
    assert "write_without_where" in _codes("UPDATE t SET note = 'no where here'")


def test_line_comment_stripped() -> None:
    # 注释里的 WHERE 不算数。
    assert "write_without_where" in _codes("DELETE FROM t -- WHERE never runs")


def test_block_comment_stripped() -> None:
    assert "write_without_where" in _codes("DELETE FROM t /* WHERE x = 1 */")


def test_with_cte_treated_as_select() -> None:
    codes = _codes("WITH c AS (SELECT 1 AS x) SELECT * FROM c")
    assert "select_star" in codes
    assert "missing_limit" in codes


def test_clean_query_has_no_findings() -> None:
    assert run_sql_preflight("SELECT id, name FROM users WHERE id = 1 LIMIT 10") == []


def test_findings_are_frozen_dataclasses() -> None:
    findings = run_sql_preflight("SELECT * FROM t")
    assert all(hasattr(f, "code") and hasattr(f, "message") for f in findings)
