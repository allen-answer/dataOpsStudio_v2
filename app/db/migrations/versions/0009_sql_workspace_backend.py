"""add SQL Workspace consoles and templates

Revision ID: 0009_sql_workspace_backend
Revises: 0008_totp_replay_guard
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_sql_workspace_backend"
down_revision: str | None = "0008_totp_replay_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sql_consoles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
            name="fk_sql_consoles_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasources.id"],
            name="fk_sql_consoles_datasource_id_datasources",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_sql_consoles_owner_updated",
        "sql_consoles",
        ["owner_user_id", "updated_at"],
    )

    op.create_table(
        "sql_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("category", sa.String(64), nullable=False, server_default="general"),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
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
            name="fk_sql_templates_project_id_projects",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_sql_templates_created_by_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_sql_templates_category_name",
        "sql_templates",
        ["category", "name"],
    )
    op.create_index("ix_sql_templates_project_id", "sql_templates", ["project_id"])

    op.add_column(
        "result_sets",
        sa.Column("console_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "result_sets",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_foreign_key(
        "fk_result_sets_console_id_sql_consoles",
        "result_sets",
        "sql_consoles",
        ["console_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_result_sets_console_updated",
        "result_sets",
        ["console_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_result_sets_console_updated", table_name="result_sets")
    op.drop_constraint(
        "fk_result_sets_console_id_sql_consoles",
        "result_sets",
        type_="foreignkey",
    )
    op.drop_column("result_sets", "updated_at")
    op.drop_column("result_sets", "console_id")
    op.drop_index("ix_sql_templates_project_id", table_name="sql_templates")
    op.drop_index("ix_sql_templates_category_name", table_name="sql_templates")
    op.drop_table("sql_templates")
    op.drop_index("ix_sql_consoles_owner_updated", table_name="sql_consoles")
    op.drop_table("sql_consoles")
