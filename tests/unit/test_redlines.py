"""红线一致性测试 —— Python 层校验契约 §4 R4/R6/R7 在 domain / db 一致。

不依赖 ast-grep(那是 source 层 lint);本测试是 Python 运行时校验。
"""

from __future__ import annotations

import ast
import pathlib

from app.db.models import APPLICATION_SECRET_KINDS, result_sets
from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS, JobKind
from app.domain.result import ResultSet
from app.domain.secret import SecretKind

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent


# ─── R4 一致性:Python SecretKind ↔ DB APPLICATION_SECRET_KINDS ───


def test_r4_secret_kind_python_and_db_consistent() -> None:
    """SecretKind enum 与 db.APPLICATION_SECRET_KINDS 必须严格一致(6 种)。"""
    py_kinds = {k.value for k in SecretKind}
    db_kinds = set(APPLICATION_SECRET_KINDS)
    assert py_kinds == db_kinds, (
        f"R4 不一致:Python={py_kinds} vs DB={db_kinds};"
        "改动一处必须同步改另一处"
    )


def test_r4_no_bootstrap_kinds_in_either() -> None:
    """Bootstrap kinds 永不可出现在 SecretKind / APPLICATION_SECRET_KINDS。"""
    forbidden = {
        "master_key",
        "pg_app_password",
        "pg_superuser_password",
        "license",
        "license_file",
    }
    py_kinds = {k.value for k in SecretKind}
    db_kinds = set(APPLICATION_SECRET_KINDS)
    assert py_kinds.isdisjoint(forbidden)
    assert db_kinds.isdisjoint(forbidden)


# ─── R6:ResultSet domain class 无 cursor 字段(Pydantic 元数据层) ───


def test_r6_resultset_pydantic_has_no_cursor_field() -> None:
    """Pydantic ResultSet 字段集禁含 cursor* 名。"""
    forbidden = {"cursor", "cursor_id", "db_cursor", "cursor_ref", "_cursor"}
    actual = set(ResultSet.model_fields.keys())
    assert actual.isdisjoint(
        forbidden
    ), f"R6 violation: ResultSet has forbidden fields {actual & forbidden}"


# ─── R6:result_sets DB 表无 cursor 字段(metadata 层) ───


def test_r6_result_sets_table_has_no_cursor_column() -> None:
    forbidden = {"cursor", "cursor_id", "db_cursor", "cursor_ref"}
    actual = set(result_sets.columns.keys())
    assert actual.isdisjoint(
        forbidden
    ), f"R6 violation: result_sets has forbidden cols {actual & forbidden}"


# ─── R6 源码 AST 层:result.py 任何类都不可定义 cursor* 字段 / 类型注解 ───


def _walk_class_field_names_and_types(
    source_path: pathlib.Path,
) -> list[tuple[str, str, str | None]]:
    """返回 [(class_name, field_name, type_annotation_repr | None), ...]。

    扫描所有 AnnAssign(`x: T = ...`) 和 Assign(`self.x = ...` 在 __init__)。
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    results: list[tuple[str, str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                # AnnAssign:类体里的 `x: T = ...`
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    type_repr = ast.unparse(stmt.annotation)
                    results.append((node.name, stmt.target.id, type_repr))
                # Assign in __init__ etc:`self.x = ...`
                if isinstance(stmt, ast.FunctionDef):
                    for inner in ast.walk(stmt):
                        if isinstance(inner, ast.Assign):
                            for target in inner.targets:
                                if (
                                    isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                ):
                                    results.append((node.name, target.attr, None))
    return results


def test_r6_result_py_source_no_cursor_field_in_any_class() -> None:
    """result.py 源代码层(AST):任何类都不可定义 cursor* 字段。

    比 Pydantic 元数据层更早 —— 即使开发者写了 `_cursor: DbCursor = ...`
    被 Pydantic 跳过/忽略,AST 层也能拦截。
    """
    result_py = PROJECT_ROOT / "app" / "domain" / "result.py"
    fields = _walk_class_field_names_and_types(result_py)
    cursor_offenders = [
        (cls, name, t)
        for cls, name, t in fields
        if "cursor" in name.lower()
    ]
    assert not cursor_offenders, (
        f"R6 violation in {result_py}: {cursor_offenders}"
    )


def test_r6_result_py_source_no_dbcursor_type_annotation() -> None:
    """result.py 任何字段类型注解不可包含 DbCursor / Cursor(防绕过命名)。"""
    result_py = PROJECT_ROOT / "app" / "domain" / "result.py"
    fields = _walk_class_field_names_and_types(result_py)
    type_offenders = [
        (cls, name, t)
        for cls, name, t in fields
        if t is not None and ("Cursor" in t or "DbCursor" in t)
    ]
    assert not type_offenders, (
        f"R6 violation in {result_py}(类型注解含 Cursor): {type_offenders}"
    )


# ─── R7 workflow 节点白名单 ───


def test_r7_workflow_whitelist_matches_contract() -> None:
    """ALLOWED_WORKFLOW_NODE_KINDS 必须与契约 §4 R7 完全一致。"""
    expected = {
        "sql_query",
        "sql_explain",
        "compare_run",
        "scenario_materialize",
        "scenario_run_all",
        "lineage_analyze",
        "export_excel",
        "notify",
        "sleep",
        "branch",
    }
    assert set(ALLOWED_WORKFLOW_NODE_KINDS) == expected


def test_r7_workflow_whitelist_excludes_dangerous_kinds() -> None:
    """白名单永不含 shell / python / http / 任意代码 / workflow_run 嵌套 / AI 调用。"""
    forbidden = {
        "shell",
        "python",
        "exec",
        "http_request",
        "workflow_run",  # 防 workflow 嵌 workflow(2.0 主线永不开放)
        "ai_assist_call",
        "ai_copilot_run",
    }
    assert set(ALLOWED_WORKFLOW_NODE_KINDS).isdisjoint(forbidden)


def test_r7_jobkind_superset_of_workflow_kinds_minus_intrinsics() -> None:
    """白名单中除 notify/sleep/branch 这三个 workflow-intrinsic 外,
    其余必须是合法 JobKind,运行时 validator 才能调度。
    """
    intrinsic = {"notify", "sleep", "branch"}
    job_kinds = {k.value for k in JobKind}
    schedulable = set(ALLOWED_WORKFLOW_NODE_KINDS) - intrinsic
    not_in_jobkinds = schedulable - job_kinds
    assert not not_in_jobkinds, (
        f"R7 inconsistent: workflow kinds {not_in_jobkinds} 不在 JobKind 中,无法调度"
    )
