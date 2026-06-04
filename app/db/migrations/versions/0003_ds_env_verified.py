"""add datasources.environment_verified column

Revision ID: 0003_ds_env_verified
Revises: 0002_datasources_database_name
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_ds_env_verified"
down_revision: str | Sequence[str] | None = "0002_datasources_database_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasources",
        sa.Column(
            "environment_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("datasources", "environment_verified")
