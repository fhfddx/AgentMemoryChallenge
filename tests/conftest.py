"""测试共享配置与夹具。"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from masm.api.app import create_app
from masm.config import Settings
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository

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


@pytest.fixture(scope="session")
def database(database_url: str) -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(database_url)


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    """测试用 Settings，配置一个已知 API Key。"""
    return Settings(database_url=database_url, api_keys=("test-key",))


@pytest.fixture()
def asset_store(tmp_path: Path) -> AssetStore:
    """本地临时对象存储。"""
    return AssetStore(tmp_path / "assets")


@pytest.fixture()
def add_service(database: Database, asset_store: AssetStore, settings: Settings) -> AddService:
    """基于测试数据库与本地对象存储的 AddService。"""
    return AddService(MemoryRepository(database), asset_store, settings)


@pytest.fixture()
def client(settings: Settings, database: Database, asset_store: AssetStore) -> TestClient:
    """带完整 /add 路由的测试客户端。"""
    return TestClient(create_app(settings, database=database, asset_store=asset_store))
