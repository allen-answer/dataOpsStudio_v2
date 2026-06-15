"""allow index metadata cache entries

Revision ID: 0013_metadata_cache_indexes
Revises: 0012_compare_tasks_run_index
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_metadata_cache_indexes"
down_revision: str | None = "0012_compare_tasks_run_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("cache_level_is_supported", "metadata_caches", type_="check")
    op.create_check_constraint(
        "cache_level_is_supported",
        "metadata_caches",
        "cache_level IN ('schemas', 'tables', 'columns', 'indexes')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM metadata_caches WHERE cache_level = 'indexes'")
    op.drop_constraint("cache_level_is_supported", "metadata_caches", type_="check")
    op.create_check_constraint(
        "cache_level_is_supported",
        "metadata_caches",
        "cache_level IN ('schemas', 'tables', 'columns')",
    )
