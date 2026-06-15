"""add compare tasks and run index

Revision ID: 0012_compare_tasks_run_index
Revises: 0011_export_download_tokens
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_compare_tasks_run_index"
down_revision: str | None = "0011_export_download_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compare_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(), nullable=False),
        sa.Column("target_ref", postgresql.JSONB(), nullable=False),
        sa.Column(
            "columns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "compare_rules",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "run_limits",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
            name="fk_compare_tasks_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["datasources.id"],
            name="fk_compare_tasks_source_id_datasources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["datasources.id"],
            name="fk_compare_tasks_target_id_datasources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_compare_tasks_created_by_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "project_id",
            "name",
            name="uq_compare_tasks_project_name",
        ),
    )
    op.create_index(
        "ix_compare_tasks_project_updated",
        "compare_tasks",
        ["project_id", "updated_at"],
    )
    op.create_index("ix_compare_tasks_source_id", "compare_tasks", ["source_id"])
    op.create_index("ix_compare_tasks_target_id", "compare_tasks", ["target_id"])

    op.create_table(
        "run_index",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("result_path", sa.Text(), nullable=True),
        sa.Column(
            "bucket_spools",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "bucket_counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "progress",
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('compare_run')",
            name="ck_run_index_kind_is_supported",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'cancelled', 'timeout')",
            name="ck_run_index_status_is_valid",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_run_index_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_run_index_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_run_index_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["compare_tasks.id"],
            name="fk_run_index_task_id_compare_tasks",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("job_id", name="uq_run_index_job_id"),
    )
    op.create_index(
        "ix_run_index_owner_project_created",
        "run_index",
        ["owner_user_id", "project_id", "created_at"],
    )
    op.create_index("ix_run_index_task_created", "run_index", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_run_index_task_created", table_name="run_index")
    op.drop_index("ix_run_index_owner_project_created", table_name="run_index")
    op.drop_table("run_index")
    op.drop_index("ix_compare_tasks_target_id", table_name="compare_tasks")
    op.drop_index("ix_compare_tasks_source_id", table_name="compare_tasks")
    op.drop_index("ix_compare_tasks_project_updated", table_name="compare_tasks")
    op.drop_table("compare_tasks")
