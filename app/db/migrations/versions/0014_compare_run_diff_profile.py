"""Add compare run diff profile.

Revision ID: 0014_compare_run_diff_profile
Revises: 0013_metadata_cache_indexes
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_compare_run_diff_profile"
down_revision: str | None = "0013_metadata_cache_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_index",
        sa.Column(
            "diff_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "run_index",
        sa.Column(
            "sample_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("run_index", "sample_result")
    op.drop_column("run_index", "diff_profile")
