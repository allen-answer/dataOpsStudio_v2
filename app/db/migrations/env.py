"""Alembic env(同步模板)。

DATABASE URL 由 DATAOPS_DATABASE_URL 环境变量提供,**不在 alembic.ini 明文**
(R8 红线)。dev 用 Makefile 组装 + POSTGRES_DEV_PASSWORD;portable / on-prem
由 launcher 读 BootstrapSecrets 后注入。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import metadata as target_metadata

# Alembic Config
config = context.config

# Logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _get_url() -> str:
    url = os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        ini_url = config.get_main_option("sqlalchemy.url")
        if ini_url:
            url = ini_url
    if not url:
        raise RuntimeError(
            "DATAOPS_DATABASE_URL 环境变量必填(alembic.ini 不写明文凭据,R8 红线)。"
            "dev 可用:make alembic-up 自动组装。"
        )
    return url


def run_migrations_offline() -> None:
    """SQL 生成模式(--sql)。"""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """实际连 PG 跑模式。"""
    connectable = create_engine(_get_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
