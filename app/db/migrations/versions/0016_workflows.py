"""workflow definition tables

Revision ID: 0016_workflows
Revises: 0015_lineage_edges
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_workflows"
down_revision: str | None = "0015_lineage_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("dag_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column("schedule_cron", sa.String(64), nullable=True),
        sa.Column(
            "schedule_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
            name="fk_workflows_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_workflows_created_by_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_workflows_project_name"),
    )
    op.create_index(
        "ix_workflows_project_updated",
        "workflows",
        ["project_id", "updated_at"],
    )

    op.create_table(
        "workflow_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("dag_jsonb", postgresql.JSONB(), nullable=False),
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
            ["created_by"],
            ["users.id"],
            name="fk_workflow_templates_created_by_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("name", name="uq_workflow_templates_name"),
    )


def downgrade() -> None:
    op.drop_table("workflow_templates")
    op.drop_index("ix_workflows_project_updated", table_name="workflows")
    op.drop_table("workflows")
