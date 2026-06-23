"""persist lineage edge tables

Revision ID: 0015_lineage_edges
Revises: 0014_compare_run_diff_profile
Create Date: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_lineage_edges"
down_revision: str | None = "0014_compare_run_diff_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lineage_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("dialect", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "parse_summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("status IN ('success', 'failed')", name="status_is_valid"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_lineage_runs_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasources.id"],
            name="fk_lineage_runs_datasource_id_datasources",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "project_id",
            "datasource_id",
            "dialect",
            "source_ref",
            "sql_hash",
            "parser_version",
            name="uq_lineage_runs_cache_key",
        ),
    )
    op.create_index(
        "ix_lineage_runs_project_hash",
        "lineage_runs",
        ["project_id", "sql_hash"],
    )
    op.create_index(
        "ix_lineage_runs_datasource_created",
        "lineage_runs",
        ["datasource_id", "created_at"],
    )

    op.create_table(
        "lineage_edges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_table", sa.String(512), nullable=False),
        sa.Column("target_table", sa.String(512), nullable=False),
        sa.Column("edge_kind", sa.String(32), nullable=False),
        sa.Column("inferred", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("inference_status", sa.String(32), nullable=False, server_default="confirmed"),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column("sql_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("edge_kind IN ('table')", name="edge_kind_is_supported"),
        sa.CheckConstraint(
            "inference_status IN ('confirmed', 'inferred', 'rejected')",
            name="inference_status_is_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["lineage_runs.id"],
            name="fk_lineage_edges_run_id_lineage_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_lineage_edges_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_lineage_edges_source_table",
        "lineage_edges",
        ["project_id", "source_table"],
    )
    op.create_index(
        "ix_lineage_edges_target_table",
        "lineage_edges",
        ["project_id", "target_table"],
    )
    op.create_index("ix_lineage_edges_run_id", "lineage_edges", ["run_id"])

    op.create_table(
        "lineage_column_edges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_table", sa.String(512), nullable=False),
        sa.Column("source_column", sa.String(256), nullable=False),
        sa.Column("target_table", sa.String(512), nullable=False),
        sa.Column("target_column", sa.String(256), nullable=False),
        sa.Column("transformation", sa.String(32), nullable=False),
        sa.Column("transformation_subtype", sa.String(32), nullable=False),
        sa.Column("inferred", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("inference_status", sa.String(32), nullable=False, server_default="confirmed"),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column("sql_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "transformation IN ('DIRECT', 'INDIRECT')",
            name="transformation_is_supported",
        ),
        sa.CheckConstraint(
            "transformation_subtype IN "
            "('DIRECT', 'AGGREGATION', 'TRANSFORMATION', 'FILTER', 'JOIN', 'CAST', 'EXPRESSION')",
            name="transformation_subtype_is_supported",
        ),
        sa.CheckConstraint(
            "inference_status IN ('confirmed', 'inferred', 'rejected')",
            name="inference_status_is_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["lineage_runs.id"],
            name="fk_lineage_column_edges_run_id_lineage_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_lineage_column_edges_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_lineage_column_edges_source",
        "lineage_column_edges",
        ["project_id", "source_table", "source_column"],
    )
    op.create_index(
        "ix_lineage_column_edges_target",
        "lineage_column_edges",
        ["project_id", "target_table", "target_column"],
    )
    op.create_index("ix_lineage_column_edges_run_id", "lineage_column_edges", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_lineage_column_edges_run_id", table_name="lineage_column_edges")
    op.drop_index("ix_lineage_column_edges_target", table_name="lineage_column_edges")
    op.drop_index("ix_lineage_column_edges_source", table_name="lineage_column_edges")
    op.drop_table("lineage_column_edges")
    op.drop_index("ix_lineage_edges_run_id", table_name="lineage_edges")
    op.drop_index("ix_lineage_edges_target_table", table_name="lineage_edges")
    op.drop_index("ix_lineage_edges_source_table", table_name="lineage_edges")
    op.drop_table("lineage_edges")
    op.drop_index("ix_lineage_runs_datasource_created", table_name="lineage_runs")
    op.drop_index("ix_lineage_runs_project_hash", table_name="lineage_runs")
    op.drop_table("lineage_runs")
