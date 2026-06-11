"""add MFA pending seed and recovery codes

Revision ID: 0006_mfa_recovery_codes
Revises: 0005_revoked_tokens
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_mfa_recovery_codes"
down_revision: str | Sequence[str] | None = "0005_revoked_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("mfa_pending_secret_ref", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mfa_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_recovery_codes")),
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_user_id"),
        "mfa_recovery_codes",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_unused"),
        "mfa_recovery_codes",
        ["user_id", "used_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mfa_recovery_codes_unused"), table_name="mfa_recovery_codes")
    op.drop_index(op.f("ix_mfa_recovery_codes_user_id"), table_name="mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")
    op.drop_column("users", "mfa_pending_secret_ref")
