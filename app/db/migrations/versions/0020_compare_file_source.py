"""compare_tasks file source: source_id/target_id nullable

C-1 文件源对比:file kind 的一侧无 datasource,故放开 source_id/target_id
非空约束(改为可空)。FK 与 RESTRICT 语义不变,仅允许 NULL。

Revision ID: 0020_compare_file_source
Revises: 0019_uploads
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_compare_file_source"
down_revision: str | None = "0019_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "compare_tasks",
        "source_id",
        existing_type=sa.String(36),
        nullable=True,
    )
    op.alter_column(
        "compare_tasks",
        "target_id",
        existing_type=sa.String(36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "compare_tasks",
        "target_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.alter_column(
        "compare_tasks",
        "source_id",
        existing_type=sa.String(36),
        nullable=False,
    )
