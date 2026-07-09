"""system settings

Revision ID: 0025_system_settings
Revises: 0024_lineage_ai_enrichments
Create Date: 2026-07-09 21:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_system_settings"
down_revision = "0024_lineage_ai_enrichments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
