"""红线一致性测试 —— Python 层校验契约 §4 R4/R6/R7 在 domain / db 一致。

不依赖 ast-grep(那是 source 层 lint);本测试是 Python 运行时校验。
"""

from __future__ import annotations

import ast
import json
import pathlib
import shutil
import subprocess
import tomllib
from typing import Any, cast

from app.db.models import APPLICATION_SECRET_KINDS, result_sets
from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS, JobKind
from app.domain.result import ResultSet
from app.domain.secret import SecretKind

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent
SG_CONFIG = PROJECT_ROOT / "tools" / "lint" / "sgconfig.yml"
R2_FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "redlines" / "r2"


def _required_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is not None:
        return executable
    for candidate in (
        PROJECT_ROOT / ".venv" / "Scripts" / f"{name}.exe",
        PROJECT_ROOT / ".venv" / "bin" / name,
    ):
        if candidate.is_file():
            return str(candidate)
    raise AssertionError(f"required lint tool is not on PATH: {name}")


def _run_r2_fixture_scan() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _required_tool("sg"),
            "scan",
            "--config",
            str(SG_CONFIG),
            "--filter",
            "^r2-",
            "--json=compact",
            ".",
        ],
        cwd=R2_FIXTURE_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _ruff_check_stdin(filename: str, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _required_tool("ruff"),
            "check",
            "--no-cache",
            "--select",
            "TID251",
            "--stdin-filename",
            filename,
            "-",
        ],
        cwd=PROJECT_ROOT,
        input=source,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ─── R2 source lint:app/** coverage + precise plaintext credential forms ───


def test_r2_rule_detects_credential_access_across_app_without_false_positives() -> None:
    """R2 CLI rule catches audited forms while preserving explicit contract seams."""
    result = _run_r2_fixture_scan()
    assert result.returncode == 1, result.stdout + result.stderr

    decoded = json.loads(result.stdout)
    assert isinstance(decoded, list)
    matches = cast(list[dict[str, Any]], decoded)
    actual = {(str(match["file"]).replace("\\", "/"), str(match["text"])) for match in matches}
    assert actual == {
        ("app/domain/unsafe_access.py", "credentials.password"),
        ("app/domain/unsafe_access.py", "credentials.old_password"),
        ("app/domain/unsafe_access.py", "credentials.smtp_password"),
        ("app/domain/unsafe_access.py", "credentials.update_password"),
        ("app/domain/unsafe_access.py", "credentials.api_key"),
        ("app/domain/unsafe_access.py", "credentials.access_token"),
        ("app/domain/unsafe_access.py", 'getattr(credentials, "token")'),
        ("app/domain/unsafe_access.py", 'getattr(credentials, "new_password")'),
        ("app/services/unsafe_access.py", "credentials.token"),
        ("app/worker.py", 'row["password"]'),
        ("app/worker.py", 'row.get("api_key")'),
    }


# ─── R3 source lint:TID251 exceptions stay primitive- and line-specific ───


def test_r3_has_no_directory_wide_tid251_ignore() -> None:
    """R1/R3 exceptions must be audited imports, never directory blankets."""
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    assert ignores == {}


def test_r3_wrong_primitive_is_rejected_inside_license_directory() -> None:
    """license may use hazmat/PyNaCl, but its path must not blanket-allow bcrypt."""
    result = _ruff_check_stdin(
        "app/infrastructure/license/probe.py",
        "import bcrypt\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "TID251" in result.stdout
    assert "bcrypt" in result.stdout


def test_r3_explicit_contract_primitive_waivers_are_accepted() -> None:
    """Legitimate R3 imports remain possible through explicit, reviewable waivers."""
    license_result = _ruff_check_stdin(
        "app/infrastructure/license/probe.py",
        "from cryptography.hazmat.primitives import hashes  # noqa: TID251\n",
    )
    secretstore_result = _ruff_check_stdin(
        "app/infrastructure/secretstore/probe.py",
        "import bcrypt  # noqa: TID251\nfrom cryptography.fernet import Fernet  # noqa: TID251\n",
    )
    assert license_result.returncode == 0, license_result.stdout + license_result.stderr
    assert secretstore_result.returncode == 0, secretstore_result.stdout + secretstore_result.stderr


def test_r3_tid251_waivers_match_the_contract_inventory() -> None:
    """Every waiver names one currently required, contract-allowed import or test seam."""
    actual: set[tuple[str, str]] = set()
    for source_root in (PROJECT_ROOT / "app", PROJECT_ROOT / "tests"):
        for source_path in source_root.rglob("*.py"):
            lines = source_path.read_text(encoding="utf-8").splitlines()
            tree = ast.parse("\n".join(lines))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import | ast.ImportFrom):
                    continue
                source_line = lines[node.lineno - 1]
                if "noqa: TID251" not in source_line:
                    continue
                actual.add((source_path.relative_to(PROJECT_ROOT).as_posix(), ast.unparse(node)))

    assert actual == {
        (
            "app/infrastructure/license/verifier.py",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey",
        ),
        (
            "app/infrastructure/license/signatures.py",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey",
        ),
        ("app/infrastructure/secretstore/local_file.py", "import bcrypt"),
        (
            "app/infrastructure/secretstore/local_file.py",
            "from cryptography.fernet import Fernet, InvalidToken",
        ),
        (
            "app/infrastructure/secretstore/v1_legacy.py",
            "from cryptography.fernet import Fernet, InvalidToken",
        ),
        (
            "tests/integration/test_migrate_from_v1_pg.py",
            "from cryptography.fernet import Fernet",
        ),
        (
            "tests/unit/test_license_verifier.py",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey",
        ),
        (
            "tests/unit/test_payload_update.py",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey",
        ),
        ("tests/unit/test_migrate_from_v1.py", "from cryptography.fernet import Fernet"),
    }


# ─── R4 一致性:Python SecretKind ↔ DB APPLICATION_SECRET_KINDS ───


def test_r4_secret_kind_python_and_db_consistent() -> None:
    """SecretKind enum 与 db.APPLICATION_SECRET_KINDS 必须严格一致(6 种)。"""
    py_kinds = {k.value for k in SecretKind}
    db_kinds = set(APPLICATION_SECRET_KINDS)
    assert py_kinds == db_kinds, (
        f"R4 不一致:Python={py_kinds} vs DB={db_kinds};改动一处必须同步改另一处"
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
    assert actual.isdisjoint(forbidden), (
        f"R6 violation: ResultSet has forbidden fields {actual & forbidden}"
    )


# ─── R6:result_sets DB 表无 cursor 字段(metadata 层) ───


def test_r6_result_sets_table_has_no_cursor_column() -> None:
    forbidden = {"cursor", "cursor_id", "db_cursor", "cursor_ref"}
    actual = set(result_sets.columns.keys())
    assert actual.isdisjoint(forbidden), (
        f"R6 violation: result_sets has forbidden cols {actual & forbidden}"
    )


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
    cursor_offenders = [(cls, name, t) for cls, name, t in fields if "cursor" in name.lower()]
    assert not cursor_offenders, f"R6 violation in {result_py}: {cursor_offenders}"


def test_r6_result_py_source_no_dbcursor_type_annotation() -> None:
    """result.py 任何字段类型注解不可包含 DbCursor / Cursor(防绕过命名)。"""
    result_py = PROJECT_ROOT / "app" / "domain" / "result.py"
    fields = _walk_class_field_names_and_types(result_py)
    type_offenders = [
        (cls, name, t)
        for cls, name, t in fields
        if t is not None and ("Cursor" in t or "DbCursor" in t)
    ]
    assert not type_offenders, f"R6 violation in {result_py}(类型注解含 Cursor): {type_offenders}"


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
