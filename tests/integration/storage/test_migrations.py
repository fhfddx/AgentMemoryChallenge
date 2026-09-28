"""数据库迁移集成测试。"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from conftest import require_masm_test_database
from masm.storage.db import Database

REPO_ROOT = Path(__file__).resolve().parents[3]

# 设计规范 7.1 要求的全部必需数据表。
REQUIRED_TABLES = {
    "users",
    "sessions",
    "source_messages",
    "assets",
    "memories",
    "memory_entities",
    "memory_events",
    "memory_relations",
    "memory_embeddings",
    "memory_conflicts",
    "processing_runs",
    "request_ledger",
}


def _alembic_config(url: str) -> Config:
    """构造指向仓库 alembic 目录与目标数据库的配置。"""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def database(database_url: str) -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(database_url)


def test_migration_upgrade_downgrade_cycle(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从空库 downgrade、upgrade 的完整循环，且只作用于已验证的 masm_test。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    # 模拟外部 DATABASE_URL 指向其他库（危险场景）。
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5433/production")
    # 用已验证的 masm_test URL 覆盖，确保 alembic/env.py 实际使用的是安全库。
    with patch.dict(os.environ, {"DATABASE_URL": validated}):
        command.downgrade(cfg, "base")
        remaining = set(inspect(engine).get_table_names())
        assert not (REQUIRED_TABLES & remaining), (
            f"downgrade 后业务表仍存在: {REQUIRED_TABLES & remaining}"
        )

        command.upgrade(cfg, "head")

    tables = set(inspect(engine).get_table_names())
    missing = REQUIRED_TABLES - tables
    assert not missing, f"upgrade head 后缺少表: {sorted(missing)}"

    with engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0003"

    # 0002 引入的冲突组唯一约束必须存在。
    constraint_names = {
        constraint["name"]
        for constraint in inspect(engine).get_unique_constraints("memory_conflicts")
    }
    assert {
        "uq_memory_conflicts_group_memory",
        "uq_memory_conflicts_group_version",
    } <= constraint_names


def test_all_required_tables_exist(database: Database) -> None:
    """迁移后所有必需数据表都存在。"""
    existing = set(inspect(database.engine).get_table_names())
    missing = REQUIRED_TABLES - existing
    assert not missing, f"缺少数据表: {sorted(missing)}"


def test_pgvector_extension_and_vector_column(database: Database) -> None:
    """pgvector 扩展和 vector 列存在。"""
    with database.engine.connect() as conn:
        ext = conn.execute(
            sa.text("SELECT extname FROM pg_extension WHERE extname='vector'")
        ).scalar()
    assert ext == "vector"

    columns = {col["name"] for col in inspect(database.engine).get_columns("memory_embeddings")}
    assert "vector" in columns


def test_embeddings_table_records_model_metadata(database: Database) -> None:
    """向量记录包含模型名、版本和维度。"""
    columns = {col["name"] for col in inspect(database.engine).get_columns("memory_embeddings")}
    assert {"model_name", "model_version", "dimensions", "vector"} <= columns


def test_every_retrievable_table_has_user_id(database: Database) -> None:
    """每条可检索记录都必须直接包含 user_id 列。"""
    inspector = inspect(database.engine)
    for table in ("memories", "memory_entities", "memory_events", "memory_relations"):
        columns = {col["name"] for col in inspector.get_columns(table)}
        assert "user_id" in columns, f"{table} 缺少 user_id 列"
