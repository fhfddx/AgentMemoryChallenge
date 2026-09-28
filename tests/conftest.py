"""测试共享配置与夹具。"""

import os

import pytest
from sqlalchemy.engine import make_url

# 存储测试默认连接串；仅当未设置 MASM_TEST_DATABASE_URL 时使用。
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5433/masm_test"


def require_masm_test_database(url: str) -> str:
    """校验连接串指向的数据库名称严格等于 masm_test，否则抛出 RuntimeError。

    错误信息只包含数据库名称，绝不包含密码或完整连接串。
    """
    name = make_url(url).database
    if name != "masm_test":
        raise RuntimeError(f"测试数据库名称必须为 masm_test，当前为 {name!r}")
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    """存储测试使用的数据库连接串，返回前先经过 masm_test 安全检查。"""
    return require_masm_test_database(
        os.environ.get("MASM_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    )
