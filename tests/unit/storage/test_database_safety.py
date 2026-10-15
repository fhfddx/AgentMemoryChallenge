"""测试数据库安全检查函数 require_masm_test_database 的单元测试。"""

import pytest
from sqlalchemy import text

from conftest import require_masm_test_database
from masm.config import Settings
from masm.storage.db import Database


def test_masm_test_url_accepted() -> None:
    """数据库名为 masm_test 的 URL 被接受并原样返回。"""
    url = "postgresql+psycopg://postgres:secret@localhost:5433/masm_test"
    assert require_masm_test_database(url) == url


def test_production_database_rejected() -> None:
    """production 数据库名被拒绝。"""
    with pytest.raises(RuntimeError):
        require_masm_test_database("postgresql+psycopg://postgres@localhost:5433/production")


def test_postgres_database_rejected() -> None:
    """postgres 数据库名被拒绝。"""
    with pytest.raises(RuntimeError):
        require_masm_test_database("postgresql+psycopg://postgres@localhost:5433/postgres")


def test_empty_database_name_rejected() -> None:
    """空数据库名被拒绝。"""
    with pytest.raises(RuntimeError):
        require_masm_test_database("postgresql+psycopg://postgres@localhost:5433")


def test_error_message_does_not_contain_password_or_url() -> None:
    """错误信息不包含密码或完整连接串。"""
    url = "postgresql+psycopg://postgres:supersecret@localhost:5433/production"
    with pytest.raises(RuntimeError) as excinfo:
        require_masm_test_database(url)
    message = str(excinfo.value)
    assert "supersecret" not in message
    assert url not in message


def test_database_exceptions_hide_bound_parameter_values() -> None:
    """SQL 失败文本不得包含任何绑定参数值。"""
    database = Database.create("sqlite+pysqlite:///:memory:")
    canary = "bound-value-must-stay-private"

    with pytest.raises(Exception) as excinfo, database.engine.connect() as connection:
        connection.execute(
            text("SELECT * FROM missing_private_table WHERE marker = :marker"),
            {"marker": canary},
        )

    assert canary not in str(excinfo.value)


def test_settings_repr_hides_connection_and_api_keys() -> None:
    """pytest 夹具失败时 Settings repr 不得泄漏连接串或鉴权值。"""
    database_url = "postgresql+psycopg://private-user:private-password@db/masm_test"
    api_key = "private-api-key"

    rendered = repr(Settings(database_url=database_url, api_keys=(api_key,)))

    assert database_url not in rendered
    assert "private-user" not in rendered
    assert "private-password" not in rendered
    assert api_key not in rendered
