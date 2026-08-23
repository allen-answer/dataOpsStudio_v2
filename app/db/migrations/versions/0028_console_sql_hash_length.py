"""Widen console statement SQL hashes to the canonical tagged SHA-256 length.

Revision ID: 0028_console_sql_hash_length
Revises: 0027_console_session_schema
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028_console_sql_hash_length"
down_revision: str | None = "0027_console_session_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "console_statements",
        "sql_hash",
        existing_type=sa.String(length=64),
        type_=sa.String(length=71),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "console_statements",
        "sql_hash",
        existing_type=sa.String(length=71),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
