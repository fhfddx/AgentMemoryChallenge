"""测试数据库安全检查函数 require_masm_test_database 的单元测试。"""

import pytest

from conftest import require_masm_test_database


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
