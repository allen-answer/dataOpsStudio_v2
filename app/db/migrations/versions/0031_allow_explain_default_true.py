"""Default allow_explain to true and backfill existing datasources.

EXPLAIN 与 SELECT 同为只读操作、同风险级,却默认关着 —— 结果是 SQL 工作区的
「执行计划」对每个新建数据源都点不动。本迁移把默认改为 true,并把存量数据源一并
放开(存量行的 false 来自旧默认值,不是用户的显式选择)。

★ 回滚(downgrade)只恢复 server_default,**不把存量行改回 false** —— 迁移之后
用户可能显式开启过,回滚时无从区分"迁移放开的"与"用户自己开的",宁可保留权限
现状也不静默收回。要收回请在数据源页逐个关闭。

Revision ID: 0031_allow_explain_default_true
Revises: 0030_lineage_runs_supersede
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0031_allow_explain_default_true"
down_revision: str | None = "0030_lineage_runs_supersede"
branch_labels: str | None = None
depends_on: str | None = None

_DEFAULT_TRUE = """'{
            "allow_select": true,
            "allow_explain": true,
            "allow_dm_explain": false,
            "allow_oracle_plan_table": false,
            "allow_schema_import": false,
            "allow_schema_save": false,
            "allow_scenario_write": false,
            "allow_record_task": false
            }'::jsonb"""

_DEFAULT_FALSE = """'{
            "allow_select": true,
            "allow_explain": false,
            "allow_dm_explain": false,
            "allow_oracle_plan_table": false,
            "allow_schema_import": false,
            "allow_schema_save": false,
            "allow_scenario_write": false,
            "allow_record_task": false
            }'::jsonb"""


def upgrade() -> None:
    op.alter_column(
        "datasources",
        "operation_policy",
        server_default=sa.text(_DEFAULT_TRUE),
    )
    op.execute(
        """
        UPDATE datasources
        SET operation_policy = jsonb_set(operation_policy, '{allow_explain}', 'true'::jsonb)
        WHERE operation_policy->>'allow_explain' = 'false'
        """
    )


def downgrade() -> None:
    # 只回退默认值,不回收既有权限(见模块 docstring)。
    op.alter_column(
        "datasources",
        "operation_policy",
        server_default=sa.text(_DEFAULT_FALSE),
    )
