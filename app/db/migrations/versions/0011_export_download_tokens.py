"""add export download tokens

Revision ID: 0011_export_download_tokens
Revises: 0010_metadata_caches
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_export_download_tokens"
down_revision: str | None = "0010_metadata_caches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_download_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "format IN ('csv', 'excel', 'json', 'sql')",
            name="ck_export_download_tokens_format_is_supported",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_export_download_tokens_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_export_download_tokens_owner_user_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_export_download_tokens_owner_created",
        "export_download_tokens",
        ["owner_user_id", "created_at"],
    )
    op.create_index(
        "ix_export_download_tokens_expires_at",
        "export_download_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_export_download_tokens_expires_at", table_name="export_download_tokens")
    op.drop_index("ix_export_download_tokens_owner_created", table_name="export_download_tokens")
    op.drop_table("export_download_tokens")
