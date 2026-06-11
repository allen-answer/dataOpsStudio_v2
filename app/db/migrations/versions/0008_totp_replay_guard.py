"""add TOTP replay guard counter

Revision ID: 0008_totp_replay_guard
Revises: 0007_ai_configs
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_totp_replay_guard"
down_revision: str | None = "0007_ai_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_used_totp_counter", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_used_totp_counter")
