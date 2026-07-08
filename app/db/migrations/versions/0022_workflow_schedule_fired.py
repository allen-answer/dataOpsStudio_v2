"""workflows.schedule_last_fired_at — cron tick 幂等防重锚点(PR-4b / ADR-0009 §1)

调度器 tick 记录"最近一次已触发的 cron 时刻"到此列;只有
most_recent_fire(cron, now) > 此值时才入队 workflow_run 并推进锚点。
同一触发点不重复入队,进程重启不补跑历史错过点(至多补当前点一次)。
旧行保持 NULL(= 从未触发;首次 tick 见到只播种基线不触发)。

Revision ID: 0022_workflow_schedule_fired
Revises: 0021_secret_refs_smtp_kind
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_workflow_schedule_fired"
down_revision: str | None = "0021_secret_refs_smtp_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("schedule_last_fired_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflows", "schedule_last_fired_at")
