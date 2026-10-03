"""Alembic 迁移环境。"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from masm.storage.models import Base

config = context.config

if config.config_file_name is not None:
    # 迁移在 API 启动前执行：保留应用已创建的聚合诊断日志器。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """优先使用 DATABASE_URL 环境变量，回退到 alembic.ini 的占位串。"""
    return os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 而不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
