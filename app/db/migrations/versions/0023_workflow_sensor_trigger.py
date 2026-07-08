"""workflows sensor 触发列 —— C-10 SQL sensor(数据到达触发 + 冷却期)

调度线程周期在 sensor.datasource 上跑只读 SQL,结果第一行第一列 truthy 即入队
workflow_run。本迁移加三列(均无命名约束/索引,故无 naming_convention 双前缀坑):

- sensor_enabled:冗余自 dag_jsonb.sensor.enabled,调度器扫表用(权威在 dag_jsonb)。
- sensor_last_checked_at:上次派发 sensor 检查 job 的时刻(检查间隔节流锚点)。
- sensor_last_triggered_at:上次命中触发的时刻(冷却期锚点,worker 带守卫原子推进)。

旧行 sensor_enabled 取 server_default false,两个锚点 NULL(= 从未发生)。

Revision ID: 0023_workflow_sensor_trigger
Revises: 0022_workflow_schedule_fired
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_workflow_sensor_trigger"
down_revision: str | None = "0022_workflow_schedule_fired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column(
            "sensor_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "workflows",
        sa.Column("sensor_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflows",
        sa.Column("sensor_last_triggered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflows", "sensor_last_triggered_at")
    op.drop_column("workflows", "sensor_last_checked_at")
    op.drop_column("workflows", "sensor_enabled")
