"""add datasource operation_policy and jobs.error_code

Revision ID: 0004_ds_policy_job_error_code
Revises: 0003_ds_env_verified
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004_ds_policy_job_error_code"
down_revision: str | Sequence[str] | None = "0003_ds_env_verified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFAULT_OPERATION_POLICY = sa.text(
    """'{
    "allow_select": true,
    "allow_explain": false,
    "allow_dm_explain": false,
    "allow_oracle_plan_table": false,
    "allow_schema_import": false,
    "allow_schema_save": false,
    "allow_scenario_write": false,
    "allow_record_task": false
    }'::jsonb"""
)


def upgrade() -> None:
    op.add_column(
        "datasources",
        sa.Column(
            "operation_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=_DEFAULT_OPERATION_POLICY,
        ),
    )
    op.add_column("jobs", sa.Column("error_code", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "error_code")
    op.drop_column("datasources", "operation_policy")
