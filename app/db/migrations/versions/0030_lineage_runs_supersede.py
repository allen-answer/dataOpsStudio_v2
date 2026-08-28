"""Keep lineage runs as history: supersede instead of delete.

Revision ID: 0030_lineage_runs_supersede
Revises: 0029_compare_result_inputs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030_lineage_runs_supersede"
down_revision: str | None = "0029_compare_result_inputs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "lineage_runs",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lineage_runs",
        sa.Column("superseded_by", sa.String(length=36), nullable=True),
    )
    # DEFERRABLE INITIALLY DEFERRED:refresh 在同一事务里"先标记旧行被 X 取代、再插入 X"
    # (顺序不能反 —— partial unique index 要求先让出生效位),即时校验会在插入前就报
    # ForeignKeyViolation。延迟到提交时校验,两行都已就位。
    op.create_foreign_key(
        op.f("fk_lineage_runs_superseded_by_lineage_runs"),
        "lineage_runs",
        "lineage_runs",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )
    # 留痕后同一 cache key 可以有多行(历史 + 生效),唯一性只约束"生效中"那一行。
    op.drop_constraint("uq_lineage_runs_cache_key", "lineage_runs", type_="unique")
    op.create_index(
        "uq_lineage_runs_cache_key_active",
        "lineage_runs",
        ["project_id", "datasource_id", "dialect", "source_ref", "sql_hash", "parser_version"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    # 解析历史列表:按项目倒序翻页。
    op.create_index(
        "ix_lineage_runs_project_created",
        "lineage_runs",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_runs_project_created", table_name="lineage_runs")
    op.drop_index("uq_lineage_runs_cache_key_active", table_name="lineage_runs")
    op.create_unique_constraint(
        "uq_lineage_runs_cache_key",
        "lineage_runs",
        ["project_id", "datasource_id", "dialect", "source_ref", "sql_hash", "parser_version"],
    )
    op.drop_constraint(
        op.f("fk_lineage_runs_superseded_by_lineage_runs"), "lineage_runs", type_="foreignkey"
    )
    op.drop_column("lineage_runs", "superseded_by")
    op.drop_column("lineage_runs", "superseded_at")
