"""add delayed job claim time

Revision ID: 0026_job_available_at
Revises: 0025_system_settings
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_job_available_at"
down_revision: str | None = "0025_system_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.drop_index("ix_jobs_queue_pending", table_name="jobs")
    op.create_index(
        "ix_jobs_queue_pending",
        "jobs",
        ["available_at", "priority", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_queue_pending", table_name="jobs")
    op.create_index(
        "ix_jobs_queue_pending",
        "jobs",
        ["priority", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("jobs", "available_at")
