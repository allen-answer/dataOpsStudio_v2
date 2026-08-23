"""app/db/models.py 静态检查 —— R4 / R6 红线 DB 层防御就位。

不连 PG,只对 metadata 对象做断言。真实迁移由 docker compose + alembic upgrade
在 Step 1.6 服务器验证。
"""

from __future__ import annotations

import importlib
import pathlib
import re
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.schema import DefaultClause

from app.db.models import (
    APPLICATION_SECRET_KINDS,
    ai_configs,
    audit_logs,
    compare_result_inputs,
    compare_tasks,
    datasources,
    export_download_tokens,
    jobs,
    license_state,
    lineage_column_edges,
    lineage_edges,
    lineage_runs,
    metadata,
    metadata_caches,
    mfa_recovery_codes,
    result_sets,
    revoked_tokens,
    run_index,
    secret_refs,
    sql_consoles,
    sql_templates,
    users,
    workflow_templates,
    workflows,
)
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile


def test_job_kind_contains_workflow_intrinsics() -> None:
    job_kinds = {kind.value for kind in JobKind}

    assert {"notify", "sleep", "branch"} <= job_kinds


def test_job_available_at_defaults_to_current_aware_utc_time() -> None:
    before = datetime.now(UTC)
    job = Job(
        id="j-available-default",
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id="u-1",
        project_id="p-1",
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id="a-1",
    )
    after = datetime.now(UTC)

    assert job.available_at.tzinfo is not None
    assert before <= job.available_at <= after


def test_job_available_at_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Job(
            id="j-available-naive",
            kind=JobKind.SQL_QUERY,
            status=JobStatus.PENDING,
            owner_user_id="u-1",
            project_id="p-1",
            priority=0,
            available_at=datetime(2026, 1, 1),
            timeout_seconds=300,
            resource_profile=ResourceProfile(),
            audit_id="a-1",
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


def test_metadata_has_31_tables() -> None:
    expected = {
        "ai_configs",
        "compare_result_inputs",
        "compare_tasks",
        "console_sessions",
        "console_statement_events",
        "console_statements",
        "export_download_tokens",
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
        "run_index",
        "sql_consoles",
        "sql_templates",
        "metadata_caches",
        "system_settings",
        "license_state",
        "lineage_ai_enrichments",  # 0022:L-7 整份报告 AI 解读(纯追加)
        "lineage_column_edges",
        "lineage_edges",
        "lineage_runs",
        "uploads",  # 0019:用户上传登记(血缘批量 ZIP / 对比文件源)
        "workflows",
        "workflow_templates",
    }
    assert set(metadata.tables.keys()) == expected


def test_metadata_cache_supports_index_cache_level() -> None:
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in metadata_caches.constraints
        if isinstance(constraint, CheckConstraint)
    )

    assert "'indexes'" in check_sql


def test_metadata_cache_indexes_migration_uses_real_check_constraint_name() -> None:
    migration_path = (
        pathlib.Path(__file__).parent.parent.parent
        / "app"
        / "db"
        / "migrations"
        / "versions"
        / "0013_metadata_cache_indexes.py"
    )
    text = migration_path.read_text(encoding="utf-8")

    assert text.count('"ck_metadata_caches_cache_level_is_supported"') == 4
    assert '"cache_level_is_supported"' not in text


def test_compare_tasks_and_run_index_schema_support_compare_run() -> None:
    task_cols = set(compare_tasks.columns.keys())
    run_cols = set(run_index.columns.keys())
    task_indexes = {idx.name for idx in compare_tasks.indexes}
    run_indexes = {idx.name for idx in run_index.indexes}
    run_check_names = {c.name for c in run_index.constraints if c.name is not None}

    assert task_cols == {
        "id",
        "project_id",
        "name",
        "source_id",
        "target_id",
        "source_ref",
        "target_ref",
        "columns",
        "compare_rules",
        "run_limits",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert run_cols == {
        "run_id",
        "kind",
        "job_id",
        "owner_user_id",
        "project_id",
        "task_id",
        "status",
        "result_path",
        "bucket_spools",
        "bucket_counts",
        "progress",
        "diff_profile",
        "sample_result",
        "created_at",
        "updated_at",
        "finished_at",
    }
    assert "ix_compare_tasks_project_updated" in task_indexes
    assert "ix_compare_tasks_source_id" in task_indexes
    assert "ix_compare_tasks_target_id" in task_indexes
    assert "ix_run_index_owner_project_created" in run_indexes
    assert "ix_run_index_task_created" in run_indexes
    assert "ck_run_index_kind_is_supported" in run_check_names
    assert "ck_run_index_status_is_valid" in run_check_names


def test_lineage_edge_store_schema_supports_adjacency_traversal() -> None:
    run_cols = set(lineage_runs.columns.keys())
    edge_cols = set(lineage_edges.columns.keys())
    column_edge_cols = set(lineage_column_edges.columns.keys())
    run_indexes = {idx.name for idx in lineage_runs.indexes}
    edge_indexes = {idx.name for idx in lineage_edges.indexes}
    column_edge_indexes = {idx.name for idx in lineage_column_edges.indexes}
    run_constraints = {c.name for c in lineage_runs.constraints if c.name is not None}
    column_constraints = {c.name for c in lineage_column_edges.constraints if c.name is not None}

    assert run_cols == {
        "id",
        "project_id",
        "datasource_id",
        "dialect",
        "source_ref",
        "sql_hash",
        "sql_text",  # 0018:边抽屉溯源用的 SQL 原文
        "parser_version",
        "status",
        "parse_summary",
        "created_at",
        "updated_at",
    }
    assert edge_cols == {
        "id",
        "run_id",
        "project_id",
        "source_table",
        "target_table",
        "edge_kind",
        "inferred",
        "inference_status",
        "confidence",
        "sql_hash",
        "created_at",
    }
    assert column_edge_cols == {
        "id",
        "run_id",
        "project_id",
        "source_table",
        "source_column",
        "target_table",
        "target_column",
        "transformation",
        "transformation_subtype",
        "inferred",
        "inference_status",
        "confidence",
        "sql_hash",
        "created_at",
    }
    assert "uq_lineage_runs_cache_key" in run_constraints
    assert "ix_lineage_runs_project_hash" in run_indexes
    assert "ix_lineage_edges_source_table" in edge_indexes
    assert "ix_lineage_edges_target_table" in edge_indexes
    assert "ix_lineage_column_edges_source" in column_edge_indexes
    assert "ix_lineage_column_edges_target" in column_edge_indexes
    assert "ck_lineage_column_edges_transformation_is_supported" in column_constraints


def test_workflow_tables_support_dag_persistence_and_schedule_scan() -> None:
    """Workflow 2.4 PR-2:dag_jsonb 存 WorkflowSpec;schedule 冗余列供调度器扫表。"""
    wf_cols = set(workflows.columns.keys())
    tpl_cols = set(workflow_templates.columns.keys())
    wf_indexes = {idx.name for idx in workflows.indexes}
    wf_constraints = {c.name for c in workflows.constraints if c.name is not None}
    tpl_constraints = {c.name for c in workflow_templates.constraints if c.name is not None}

    assert wf_cols == {
        "id",
        "project_id",
        "name",
        "dag_jsonb",
        "schedule_cron",
        "schedule_enabled",
        "schedule_last_fired_at",
        "sensor_enabled",
        "sensor_last_checked_at",
        "sensor_last_triggered_at",
        "enabled",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert tpl_cols == {
        "id",
        "name",
        "description",
        "dag_jsonb",
        "created_by",
        "created_at",
        "updated_at",
    }
    # 调度器扫表用的冗余列(PR-4):cron 可空、开关非空
    assert workflows.columns["schedule_cron"].nullable is True
    assert workflows.columns["schedule_enabled"].nullable is False
    # PR-4b:cron tick 幂等防重锚点,可空(NULL = 从未触发)
    assert workflows.columns["schedule_last_fired_at"].nullable is True
    # C-10 sensor:开关非空冗余列 + 检查/触发锚点可空(NULL = 从未发生)
    assert workflows.columns["sensor_enabled"].nullable is False
    assert workflows.columns["sensor_last_checked_at"].nullable is True
    assert workflows.columns["sensor_last_triggered_at"].nullable is True
    assert workflows.columns["enabled"].nullable is False
    assert workflows.columns["dag_jsonb"].nullable is False
    assert workflow_templates.columns["dag_jsonb"].nullable is False
    # R4:定义表不出现任何密码 / secret 列
    forbidden = {"password", "secret", "token", "api_key"}
    assert not {c for c in wf_cols | tpl_cols for f in forbidden if f in c}
    assert "uq_workflows_project_name" in wf_constraints
    assert "uq_workflow_templates_name" in tpl_constraints
    assert "ix_workflows_project_updated" in wf_indexes


def test_export_download_tokens_schema_is_one_time_and_ttl_indexed() -> None:
    cols = set(export_download_tokens.columns.keys())
    indexes = {idx.name for idx in export_download_tokens.indexes}
    check_names = {c.name for c in export_download_tokens.constraints if c.name is not None}

    assert cols == {
        "token_hash",
        "job_id",
        "owner_user_id",
        "format",
        "filename",
        "content_type",
        "expires_at",
        "consumed_at",
        "created_at",
    }
    assert export_download_tokens.columns["token_hash"].primary_key is True
    assert export_download_tokens.columns["expires_at"].nullable is False
    assert export_download_tokens.columns["consumed_at"].nullable is True
    assert "token" not in cols
    assert "ck_export_download_tokens_format_is_supported" in check_names
    assert "ix_export_download_tokens_owner_created" in indexes
    assert "ix_export_download_tokens_expires_at" in indexes


def test_sql_workspace_tables_support_console_and_templates() -> None:
    console_cols = set(sql_consoles.columns.keys())
    template_cols = set(sql_templates.columns.keys())
    console_indexes = {idx.name for idx in sql_consoles.indexes}
    template_indexes = {idx.name for idx in sql_templates.indexes}

    assert console_cols == {
        "id",
        "owner_user_id",
        "datasource_id",
        "name",
        "sql",
        "pinned",
        "session_epoch",
        "created_at",
        "updated_at",
    }
    assert template_cols == {
        "id",
        "name",
        "description",
        "sql_text",
        "variables",
        "category",
        "project_id",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert "ix_sql_consoles_owner_updated" in console_indexes
    assert "ix_sql_templates_category_name" in template_indexes
    assert "ix_sql_templates_project_id" in template_indexes


def test_metadata_caches_schema_supports_ttl_and_object_keys() -> None:
    cols = set(metadata_caches.columns.keys())
    indexes = {idx.name for idx in metadata_caches.indexes}
    check_names = {c.name for c in metadata_caches.constraints if c.name is not None}

    assert cols == {
        "id",
        "datasource_id",
        "cache_level",
        "schema_name",
        "table_name",
        "payload",
        "refreshed_at",
        "expires_at",
        "created_at",
        "updated_at",
    }
    assert metadata_caches.columns["payload"].nullable is False
    assert metadata_caches.columns["expires_at"].nullable is False
    assert "uq_metadata_caches_key" in check_names
    assert "ck_metadata_caches_cache_level_is_supported" in check_names
    assert "ix_metadata_caches_datasource_level" in indexes
    assert "ix_metadata_caches_expires_at" in indexes


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
    assert "last_used_totp_counter" in user_cols
    assert code_cols == {"id", "user_id", "code_hash", "created_at", "used_at"}
    assert users.columns["last_used_totp_counter"].nullable is True
    assert mfa_recovery_codes.columns["code_hash"].nullable is False
    assert "ix_mfa_recovery_codes_user_id" in index_names
    assert "ix_mfa_recovery_codes_unused" in index_names


def test_ai_configs_schema_stores_key_by_secret_ref_only() -> None:
    cols = set(ai_configs.columns.keys())
    check_names = {c.name for c in ai_configs.constraints if c.name is not None}
    check_sql = {
        c.name: str(c.sqltext)
        for c in ai_configs.constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }

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
    assert check_sql["ck_ai_configs_max_auto_egress_level_range"].endswith("<= 3")


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
    # 必须正好 7 种,与 app.domain.secret.SecretKind 对齐
    assert len(APPLICATION_SECRET_KINDS) == 7


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
    pending_index = next(idx for idx in jobs.indexes if idx.name == "ix_jobs_queue_pending")

    assert {column.name for column in pending_index.columns} == {
        "available_at",
        "priority",
        "created_at",
    }


def test_jobs_available_at_column_is_required_and_timezone_aware() -> None:
    column = jobs.columns["available_at"]

    assert column.nullable is False
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert isinstance(column.server_default, DefaultClause)
    assert str(column.server_default.arg) == "now()"


def test_job_available_at_migration_replaces_pending_queue_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.db.migrations.versions.0026_job_available_at")
    operation = MagicMock()
    monkeypatch.setattr(migration, "op", operation)

    assert migration.revision == "0026_job_available_at"
    assert migration.down_revision == "0025_system_settings"

    migration.upgrade()

    assert [called[0] for called in operation.method_calls] == [
        "add_column",
        "drop_index",
        "create_index",
    ]
    added_column = operation.add_column.call_args.args[1]
    assert added_column.name == "available_at"
    assert added_column.nullable is False
    assert added_column.type.timezone is True
    assert str(added_column.server_default.arg) == "now()"
    assert operation.create_index.call_args.args[:3] == (
        "ix_jobs_queue_pending",
        "jobs",
        ["available_at", "priority", "created_at"],
    )

    operation.reset_mock()
    migration.downgrade()

    assert [called[0] for called in operation.method_calls] == [
        "drop_index",
        "create_index",
        "drop_column",
    ]
    assert operation.create_index.call_args.args[:3] == (
        "ix_jobs_queue_pending",
        "jobs",
        ["priority", "created_at"],
    )
    operation.drop_column.assert_called_once_with("jobs", "available_at")


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


def test_compare_result_inputs_enforce_origin_and_state_values() -> None:
    columns = set(compare_result_inputs.columns.keys())
    assert {
        "project_id",
        "created_by",
        "origin_kind",
        "origin_id",
        "source_result_set_id",
        "columns",
        "loaded_rows",
        "total_rows",
        "truncated",
        "has_more",
        "state",
        "expires_at",
    } <= columns
    check_names = {
        constraint.name
        for constraint in compare_result_inputs.constraints
        if constraint.name is not None
    }
    assert "ck_compare_result_inputs_origin_kind_is_supported" in check_names
    assert "ck_compare_result_inputs_state_is_supported" in check_names
