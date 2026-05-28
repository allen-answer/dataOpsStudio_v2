"""initial schema (10 tables)

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-28

★ secret_ref 字段全部 VARCHAR(64) **无 FK**(cross-storage 逻辑引用):
  KMS 实现下 secret 不在 PG,加 FK 会让 KMS 实现违反约束。
  任何后续 PR 试图给 *_secret_ref 字段加 FK 必须先改架构。

★ R4 红线 DB 层防御:secret_refs CHECK 限定 kind 为 Application Secret(6 种)。
  Bootstrap kind(master_key / pg_*_password / license)永不可插入。

★ R6 红线 DB 层防御:result_sets 表不含 cursor 字段。任何后续 migration 若加
  cursor* 字段需先改 ResultSet domain + 契约 §3.4(预计不应该)。

★ FOR UPDATE SKIP LOCKED 配套:jobs 偏序索引 ix_jobs_queue_pending,
  WHERE status='pending',极小且只覆盖待处理任务。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        # ★ no FK on mfa_secret_ref(cross-storage,见模块注释)
        sa.Column("mfa_secret_ref", sa.String(64), nullable=True),
        sa.Column("role", sa.String(32), nullable=False, server_default="viewer"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    # ── projects ───────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_projects_owner_user_id_users",
            ondelete="RESTRICT",
        ),
    )

    # ── project_members ────────────────────────────────────────────────────
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_members_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_project_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_members_project_user"
        ),
    )

    # ── datasources ────────────────────────────────────────────────────────
    op.create_table(
        "datasources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("db_type", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        # ★ NO FK on password_secret_ref(cross-storage,见模块注释)
        sa.Column("password_secret_ref", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False, server_default="dev"),
        sa.Column(
            "capability_profile",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_datasources_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_datasources_project_name"),
    )

    # ── jobs ───────────────────────────────────────────────────────────────
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column(
            "datasource_ids",
            postgresql.ARRAY(sa.String(36)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "resource_profile",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True),
        sa.Column("audit_id", sa.String(36), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("cancel_reason", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("parent_workflow_run_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_jobs_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_jobs_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    # ★ 关键索引:status='pending' 偏序索引,FOR UPDATE SKIP LOCKED 走这条
    op.create_index(
        "ix_jobs_queue_pending",
        "jobs",
        ["priority", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # worker 心跳监控
    op.create_index(
        "ix_jobs_worker_heartbeat",
        "jobs",
        ["worker_id", "last_heartbeat"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_jobs_owner_user_id", "jobs", ["owner_user_id", "created_at"])
    op.create_index("ix_jobs_project_id", "jobs", ["project_id", "created_at"])

    # ── job_events ─────────────────────────────────────────────────────────
    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_job_events_job_id_jobs", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_job_events_job_ts", "job_events", ["job_id", "ts"])

    # ── secret_refs ────────────────────────────────────────────────────────
    # ★ R4:CHECK 限定 kind 为 Application Secret 6 种;Bootstrap kind 永禁
    op.create_table(
        "secret_refs",
        sa.Column("ref", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),  # 跨存储,无 FK
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rotation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint(
            "kind IN ('datasource_password', 'ai_api_key', 'mfa_totp_seed', "
            "'oauth_token', 'webhook_secret', 'signed_url_secret')",
            name="ck_secret_refs_kind_is_application_secret",
        ),
    )
    op.create_index("ix_secret_refs_kind", "secret_refs", ["kind"])

    # ── audit_logs ─────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),  # 关联运行日志
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
    )
    op.execute("CREATE INDEX ix_audit_logs_ts ON audit_logs (ts DESC)")
    op.execute(
        "CREATE INDEX ix_audit_logs_user_id_ts ON audit_logs (user_id, ts DESC)"
    )
    op.create_index(
        "ix_audit_logs_resource",
        "audit_logs",
        ["resource_type", "resource_id"],
    )
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])
    op.execute(
        "CREATE INDEX ix_audit_logs_action_ts ON audit_logs (action, ts DESC)"
    )

    # ── result_sets ────────────────────────────────────────────────────────
    # ★ R6:本表禁有任何 cursor* 字段。
    op.create_table(
        "result_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("storage_ref", postgresql.JSONB(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("loaded_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column(
            "state", sa.String(32), nullable=False, server_default="streaming"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('streaming', 'complete', 'failed', 'closed')",
            name="ck_result_sets_state_is_valid",
        ),
    )
    op.create_index(
        "ix_result_sets_execution_id", "result_sets", ["execution_id"]
    )

    # ── license_state ──────────────────────────────────────────────────────
    op.create_table(
        "license_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("edition", sa.String(32), nullable=True),
        sa.Column("customer", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "features",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("signature_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mode", sa.String(32), nullable=False, server_default="trial"),
        sa.Column("repair_reason", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_license_state_singleton"),
        sa.CheckConstraint(
            "mode IN ('trial', 'valid', 'in_grace', 'repair')",
            name="ck_license_state_mode_is_valid",
        ),
    )


def downgrade() -> None:
    # 反序 drop(尊重 FK 依赖)
    op.drop_table("license_state")
    op.drop_index("ix_result_sets_execution_id", table_name="result_sets")
    op.drop_table("result_sets")
    op.drop_index("ix_audit_logs_action_ts", table_name="audit_logs")
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_id_ts", table_name="audit_logs")
    op.drop_index("ix_audit_logs_ts", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_secret_refs_kind", table_name="secret_refs")
    op.drop_table("secret_refs")
    op.drop_index("ix_job_events_job_ts", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_project_id", table_name="jobs")
    op.drop_index("ix_jobs_owner_user_id", table_name="jobs")
    op.drop_index("ix_jobs_worker_heartbeat", table_name="jobs")
    op.drop_index("ix_jobs_queue_pending", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("datasources")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("users")
