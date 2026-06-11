"""app/db/models.py 静态检查 —— R4 / R6 红线 DB 层防御就位。

不连 PG,只对 metadata 对象做断言。真实迁移由 docker compose + alembic upgrade
在 Step 1.6 服务器验证。
"""

from __future__ import annotations

import pathlib
import re

from app.db.models import (
    APPLICATION_SECRET_KINDS,
    ai_configs,
    audit_logs,
    datasources,
    jobs,
    license_state,
    metadata,
    mfa_recovery_codes,
    result_sets,
    revoked_tokens,
    secret_refs,
    users,
)


def test_datasources_has_database_name_column() -> None:
    """migration 0002 加的 database_name 列必须在 metadata 中
    (防 models.py 与 migration 漂移;DatasourceConnInfo.database 映射至此)。
    """
    cols = set(datasources.columns.keys())
    assert "database_name" in cols
    assert datasources.columns["database_name"].nullable is True


def test_datasources_has_environment_verified_column() -> None:
    """T7 list response exposes whether environment labeling was verified."""

    cols = set(datasources.columns.keys())
    assert "environment_verified" in cols
    assert datasources.columns["environment_verified"].nullable is False


def test_datasources_has_operation_policy_column() -> None:
    cols = set(datasources.columns.keys())
    assert "operation_policy" in cols
    assert datasources.columns["operation_policy"].nullable is False


def test_all_alembic_revision_ids_under_32_chars() -> None:
    """alembic_version.version_num 默认 VARCHAR(32),revision ID 过长会在
    UPDATE alembic_version 时触发 StringDataRightTruncation。

    历史教训:0002_add_datasources_database_name(34 字符)在 CI PG 上挂,
    截短到 0002_datasources_database_name(30 字符)才过。
    """
    versions_dir = (
        pathlib.Path(__file__).parent.parent.parent / "app" / "db" / "migrations" / "versions"
    )
    pattern = re.compile(r'^revision:\s*str\s*=\s*["\'](.+)["\']', re.MULTILINE)
    violations: list[tuple[str, str, int]] = []
    for f in versions_dir.glob("[0-9]*.py"):
        m = pattern.search(f.read_text(encoding="utf-8"))
        if m:
            rid = m.group(1)
            if len(rid) > 32:
                violations.append((f.name, rid, len(rid)))
    assert not violations, f"revision ID 超 32 字符: {violations}"


def test_metadata_has_13_tables() -> None:
    expected = {
        "ai_configs",
        "users",
        "mfa_recovery_codes",
        "revoked_tokens",
        "projects",
        "project_members",
        "datasources",
        "jobs",
        "job_events",
        "secret_refs",
        "audit_logs",
        "result_sets",
        "license_state",
    }
    assert set(metadata.tables.keys()) == expected


def test_revoked_tokens_schema_supports_jti_denylist_and_user_cutoff() -> None:
    user_cols = set(users.columns.keys())
    token_cols = set(revoked_tokens.columns.keys())
    index_names = {idx.name for idx in revoked_tokens.indexes}

    assert "tokens_revoked_after" in user_cols
    assert token_cols == {"jti", "user_id", "revoked_at", "expires_at"}
    assert revoked_tokens.columns["jti"].primary_key is True
    assert "ix_revoked_tokens_user_id" in index_names
    assert "ix_revoked_tokens_expires_at" in index_names


def test_mfa_recovery_codes_schema_supports_one_time_codes() -> None:
    user_cols = set(users.columns.keys())
    code_cols = set(mfa_recovery_codes.columns.keys())
    index_names = {idx.name for idx in mfa_recovery_codes.indexes}

    assert "mfa_pending_secret_ref" in user_cols
    assert code_cols == {"id", "user_id", "code_hash", "created_at", "used_at"}
    assert mfa_recovery_codes.columns["code_hash"].nullable is False
    assert "ix_mfa_recovery_codes_user_id" in index_names
    assert "ix_mfa_recovery_codes_unused" in index_names


def test_ai_configs_schema_stores_key_by_secret_ref_only() -> None:
    cols = set(ai_configs.columns.keys())
    check_names = {c.name for c in ai_configs.constraints if c.name is not None}

    assert cols == {
        "id",
        "enabled",
        "provider",
        "model",
        "base_url",
        "api_key_secret_ref",
        "max_auto_egress_level",
        "l4_requires_optin",
        "enable_inference",
        "enable_auto_translation",
        "created_at",
        "updated_at",
    }
    assert "api_key" not in cols
    assert ai_configs.columns["api_key_secret_ref"].nullable is True
    assert len(ai_configs.columns["api_key_secret_ref"].foreign_keys) == 0
    assert "ck_ai_configs_singleton" in check_names
    assert "ck_ai_configs_provider_is_supported" in check_names
    assert "ck_ai_configs_max_auto_egress_level_range" in check_names


def test_r6_result_sets_has_no_cursor_field() -> None:
    """R6 红线 DB 层:result_sets 表禁有任何 cursor* 字段。"""
    forbidden = {"cursor", "cursor_id", "db_cursor", "cursor_ref"}
    actual = set(result_sets.columns.keys())
    assert actual.isdisjoint(forbidden), (
        f"R6 violation: result_sets has forbidden cols {actual & forbidden}"
    )


def test_r4_application_secret_kinds_exclude_bootstrap() -> None:
    """R4 红线:APPLICATION_SECRET_KINDS 不含任何 Bootstrap kind。"""
    forbidden_bootstrap = {
        "master_key",
        "pg_app_password",
        "pg_superuser_password",
        "license",
        "license_file",
    }
    assert set(APPLICATION_SECRET_KINDS).isdisjoint(forbidden_bootstrap)
    # 必须正好 6 种,与 app.domain.secret.SecretKind 对齐
    assert len(APPLICATION_SECRET_KINDS) == 6


def test_secret_refs_check_constraint_exists() -> None:
    """secret_refs 表 CHECK 约束存在 —— R4 DB 层防御。"""
    check_names = {c.name for c in secret_refs.constraints if c.name is not None}
    assert "ck_secret_refs_kind_is_application_secret" in check_names


def test_secret_ref_columns_have_no_foreign_key() -> None:
    """secret_ref 字段全部无 FK —— KMS 跨存储引用,加 FK 会让 KMS 实现违反约束。"""
    cases = [
        (users, "mfa_secret_ref"),
        (users, "mfa_pending_secret_ref"),
        (ai_configs, "api_key_secret_ref"),
        (datasources, "password_secret_ref"),
        (secret_refs, "ref"),  # PK 本身,自然无 FK
        (secret_refs, "created_by"),  # 跨存储 user_id 引用
        (audit_logs, "user_id"),
        (audit_logs, "project_id"),
    ]
    for table, col_name in cases:
        col = table.columns[col_name]
        assert len(col.foreign_keys) == 0, (
            f"{table.name}.{col_name} has FK; cross-storage ref must be plain VARCHAR"
        )


def test_jobs_queue_pending_partial_index_exists() -> None:
    """FOR UPDATE SKIP LOCKED 用的偏序索引必须存在(契约 §2.2.2)。"""
    index_names = {idx.name for idx in jobs.indexes}
    assert "ix_jobs_queue_pending" in index_names


def test_jobs_has_required_queue_fields() -> None:
    """契约 §3.6 + PG queue 抢任务必备字段。"""
    cols = set(jobs.columns.keys())
    required = {
        "status",
        "priority",
        "worker_id",
        "last_heartbeat",
        "started_at",
        "finished_at",
        "cancel_requested",
        "retry_count",
        "resource_profile",  # Step 0 补
        "audit_id",
        "error_code",
        "parent_workflow_run_id",
    }
    missing = required - cols
    assert not missing, f"jobs missing required fields: {missing}"


def test_audit_logs_has_request_id_for_correlation() -> None:
    """设计稿 §9.2.1:audit 与运行日志通过 request_id 关联。"""
    assert "request_id" in audit_logs.columns


def test_license_state_singleton_and_mode_check() -> None:
    """license_state 单例 + mode 限定 4 个值(契约 §8.4 + §8.5 Repair)。"""
    check_names = {c.name for c in license_state.constraints if c.name is not None}
    assert "ck_license_state_singleton" in check_names
    assert "ck_license_state_mode_is_valid" in check_names


def test_result_sets_state_check_exists() -> None:
    """result_sets.state 限定 4 个值(streaming / complete / failed / closed)。"""
    check_names = {c.name for c in result_sets.constraints if c.name is not None}
    assert "ck_result_sets_state_is_valid" in check_names
