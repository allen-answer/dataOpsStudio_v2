"""Add immutable SQL-result inputs for Compare.

Revision ID: 0029_compare_result_inputs
Revises: 0028_console_sql_hash_length
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_compare_result_inputs"
down_revision: str | None = "0028_console_sql_hash_length"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "compare_result_inputs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("origin_kind", sa.String(length=16), nullable=False),
        sa.Column("origin_id", sa.String(length=36), nullable=False),
        sa.Column("source_result_set_id", sa.String(length=36), nullable=False),
        sa.Column(
            "columns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("loaded_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_more", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="ready", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "origin_kind IN ('statement', 'job')",
            name=op.f("ck_compare_result_inputs_origin_kind_is_supported"),
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'deleted')",
            name=op.f("ck_compare_result_inputs_state_is_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_compare_result_inputs_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_compare_result_inputs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compare_result_inputs")),
    )
    op.create_index(
        "ix_compare_result_inputs_project_created",
        "compare_result_inputs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_compare_result_inputs_expiry",
        "compare_result_inputs",
        ["state", "expires_at"],
    )
    op.create_index(
        "ix_compare_result_inputs_origin",
        "compare_result_inputs",
        ["origin_kind", "origin_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_compare_result_inputs_origin", table_name="compare_result_inputs")
    op.drop_index("ix_compare_result_inputs_expiry", table_name="compare_result_inputs")
    op.drop_index("ix_compare_result_inputs_project_created", table_name="compare_result_inputs")
    op.drop_table("compare_result_inputs")
