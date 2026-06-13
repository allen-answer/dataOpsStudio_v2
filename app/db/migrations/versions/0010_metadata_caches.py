"""add metadata browser cache

Revision ID: 0010_metadata_caches
Revises: 0009_sql_workspace_backend
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_metadata_caches"
down_revision: str | None = "0009_sql_workspace_backend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_caches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("cache_level", sa.String(32), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "cache_level IN ('schemas', 'tables', 'columns')",
            name="ck_metadata_caches_cache_level_is_supported",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasources.id"],
            name="fk_metadata_caches_datasource_id_datasources",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "datasource_id",
            "cache_level",
            "schema_name",
            "table_name",
            name="uq_metadata_caches_key",
        ),
    )
    op.create_index(
        "ix_metadata_caches_datasource_level",
        "metadata_caches",
        ["datasource_id", "cache_level"],
    )
    op.create_index("ix_metadata_caches_expires_at", "metadata_caches", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_metadata_caches_expires_at", table_name="metadata_caches")
    op.drop_index("ix_metadata_caches_datasource_level", table_name="metadata_caches")
    op.drop_table("metadata_caches")
