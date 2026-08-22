"""add Session Broker console session schema

Revision ID: 0027_console_session_schema
Revises: 0026_job_available_at
Create Date: 2026-08-22

CHECK 约束使用逻辑名 ``state_is_supported``；metadata naming_convention
负责补上 ``ck_<table>_``。直接写物理全名会产生双前缀。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_console_session_schema"
down_revision: str | None = "0026_job_available_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATES = "state IN ('connecting', 'idle', 'executing', 'cancelling', 'closing')"


def upgrade() -> None:
    op.add_column(
        "sql_consoles",
        sa.Column("session_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "console_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("console_id", sa.String(36), nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("broker_boot_id", sa.String(36), nullable=False),
        sa.Column("db_session_marker", sa.String(64), nullable=True),
        sa.Column("server_cancel", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("autocommit", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "state IN ('connecting', 'idle', 'executing', 'cancelling', 'closing', "
            "'closed', 'session_lost', 'connect_failed')",
            name="state_is_supported",
        ),
        sa.ForeignKeyConstraint(
            ["console_id"],
            ["sql_consoles.id"],
            name="fk_console_sessions_console_id_sql_consoles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasources.id"],
            name="fk_console_sessions_datasource_id_datasources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_console_sessions_owner_user_id_users",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_console_active_session",
        "console_sessions",
        ["console_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATES),
    )
    op.create_index(
        "ix_console_sessions_boot",
        "console_sessions",
        ["broker_boot_id"],
        postgresql_where=sa.text(_ACTIVE_STATES),
    )

    op.create_table(
        "console_statements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("console_id", sa.String(36), nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(64), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.String(64), nullable=False),
        sa.Column("sql_len", sa.Integer(), nullable=False),
        sa.Column("statement_kind", sa.String(16), nullable=False),
        sa.Column("is_write", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("result_set_id", sa.String(36), nullable=True),
        sa.Column("rows_affected", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("script_id", sa.String(36), nullable=True),
        sa.Column("script_seq", sa.Integer(), nullable=True),
        sa.Column("resolved_by", sa.String(36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('accepted', 'executing', 'streaming', 'succeeded', 'failed', "
            "'cancelled', 'timeout', 'outcome_unknown', 'skipped')",
            name="state_is_supported",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["console_sessions.id"],
            name="fk_console_statements_session_id_console_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["result_set_id"],
            ["result_sets.id"],
            name="fk_console_statements_result_set_id_result_sets",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_console_statements_session_request",
        ),
    )

    op.create_table(
        "console_statement_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.String(36), nullable=False),
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
            ["statement_id"],
            ["console_statements.id"],
            name="fk_console_statement_events_statement_id_console_statements",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_console_statement_events_statement_ts",
        "console_statement_events",
        ["statement_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_console_statement_events_statement_ts",
        table_name="console_statement_events",
    )
    op.drop_table("console_statement_events")
    op.drop_table("console_statements")
    op.drop_index("ix_console_sessions_boot", table_name="console_sessions")
    op.drop_index("uq_console_active_session", table_name="console_sessions")
    op.drop_table("console_sessions")
    op.drop_column("sql_consoles", "session_epoch")
