"""数据库迁移集成测试。"""

import pytest
from sqlalchemy import inspect

from masm.storage.db import Database

# 测试数据库连接串，可用 MASM_TEST_DATABASE_URL 覆盖。
TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5433/masm_test"

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


@pytest.fixture(scope="module")
def database() -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(TEST_DATABASE_URL)


def test_all_required_tables_exist(database: Database) -> None:
    """迁移后所有必需数据表都存在。"""
    existing = set(inspect(database.engine).get_table_names())
    missing = REQUIRED_TABLES - existing
    assert not missing, f"缺少数据表: {sorted(missing)}"


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
