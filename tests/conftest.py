"""测试共享配置与夹具。"""

import os

import pytest

# 存储测试默认连接串；仅当未设置 MASM_TEST_DATABASE_URL 时使用。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5433/masm_test"


@pytest.fixture(scope="session")
def database_url() -> str:
    """存储测试使用的数据库连接串，优先读取 MASM_TEST_DATABASE_URL。"""
    return os.environ.get("MASM_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
