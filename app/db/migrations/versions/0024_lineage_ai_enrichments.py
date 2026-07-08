"""lineage_ai_enrichments table

L-7:整份血缘报告的 AI 解读(风险点 / 刷新模式解释 / 建议)。纯追加存储 —— 每次
ai-enrich 落一行,绝不回写 lineage_runs.parse_summary 的确定性字段。

Revision ID: 0024_lineage_ai_enrichments
Revises: 0023_workflow_sensor_trigger
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_lineage_ai_enrichments"
down_revision: str | None = "0023_workflow_sensor_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lineage_ai_enrichments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("egress_level", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["lineage_runs.id"],
            name="fk_lineage_ai_enrichments_run_id_lineage_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_lineage_ai_enrichments_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_lineage_ai_enrichments_run_created",
        "lineage_ai_enrichments",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lineage_ai_enrichments_run_created",
        table_name="lineage_ai_enrichments",
    )
    op.drop_table("lineage_ai_enrichments")
