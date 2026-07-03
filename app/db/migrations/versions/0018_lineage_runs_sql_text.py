"""lineage_runs sql_text

边详情抽屉需要展示"这条边来自哪段 SQL";此前只存 sql_hash。
旧 run 无法回填,保持 NULL(前端显示"SQL 未存档")。

Revision ID: 0018_lineage_runs_sql_text
Revises: 0017_jobs_workflow_node_unique
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_lineage_runs_sql_text"
down_revision: str | None = "0017_jobs_workflow_node_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lineage_runs", sa.Column("sql_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("lineage_runs", "sql_text")
