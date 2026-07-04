"""uploads table

用户上传文件登记(血缘批量 ZIP / 对比文件源)。文件本体在 result_store
uploads/ 目录由 upload_ttl_hours GC;本表行是所有权与元信息登记。

Revision ID: 0019_uploads
Revises: 0018_lineage_runs_sql_text
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_uploads"
down_revision: str | None = "0018_lineage_runs_sql_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_uploads_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_uploads_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "purpose IN ('lineage_batch', 'compare_source')",
            name="ck_uploads_purpose_is_supported",
        ),
    )
    op.create_index("ix_uploads_project_created", "uploads", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_uploads_project_created", table_name="uploads")
    op.drop_table("uploads")
