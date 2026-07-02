"""jobs workflow-node unique index

同一 workflow run 内同一节点只允许一个子 job(stale worker 苏醒后的
重复 enqueue 撞唯一索引被跳过,防节点双跑);前导列同时充当
list_jobs_by_parent 的邻接索引。

Revision ID: 0017_jobs_workflow_node_unique
Revises: 0016_workflows
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_jobs_workflow_node_unique"
down_revision: str | None = "0016_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_jobs_workflow_node_per_run",
        "jobs",
        ["parent_workflow_run_id", sa.text("(payload->>'workflow_node_id')")],
        unique=True,
        postgresql_where=sa.text("parent_workflow_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_workflow_node_per_run", table_name="jobs")
