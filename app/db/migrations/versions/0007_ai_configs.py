"""add ai_configs table

Revision ID: 0007_ai_configs
Revises: 0006_mfa_recovery_codes
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_ai_configs"
down_revision: str | None = "0006_mfa_recovery_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="off", nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key_secret_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "max_auto_egress_level",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "l4_requires_optin",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "enable_inference",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "enable_auto_translation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_ai_configs_singleton")),
        sa.CheckConstraint(
            "provider IN ('off', 'mock', 'openai_compatible', 'anthropic', 'ollama')",
            name=op.f("ck_ai_configs_provider_is_supported"),
        ),
        sa.CheckConstraint(
            "max_auto_egress_level >= 0 AND max_auto_egress_level <= 4",
            name=op.f("ck_ai_configs_max_auto_egress_level_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_configs")),
    )


def downgrade() -> None:
    op.drop_table("ai_configs")
