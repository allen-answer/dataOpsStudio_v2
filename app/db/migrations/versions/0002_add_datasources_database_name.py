"""add datasources.database_name column

Revision ID: 0002_add_datasources_database_name
Revises: 0001_initial
Create Date: 2026-05-28

修正 Step 1 建 datasources 表时漏的"业务连接的库名/schema/service"列。
DatasourceConnInfo.database 字段直接映射到本列。

★ 列名 database_name 而非 database —— `database` 是 SQL 保留字,做列名时
  需要双引号 quote,易在不同 driver / 工具下埋坑。domain 层字段名仍叫
  database,应用映射层做名字翻译(domain.database <-> db.database_name)。

★ NULL 而非 NOT NULL DEFAULT '':
  - 现有行(若有)免改;
  - 应用层 Pydantic 验证 min_length=1 保证非空,DB 层不放 '' garbage;
  - DEFAULT '' 会让 silent 默认值进库,调试难。

★ 不加 CHECK 约束:与 secret_refs.kind(R4 必拦)不同,
  database_name 是非安全敏感字段,新方言加列要改 CHECK 太贵;
  应用层 Pydantic 验证已足。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_datasources_database_name"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datasources",
        sa.Column("database_name", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("datasources", "database_name")
