"""Health 契约测试。"""

from fastapi.testclient import TestClient

from masm.api.app import create_app
from masm.config import Settings


def _make_client() -> TestClient:
    """构建一个未配置任何认证密钥的测试客户端。"""
    settings = Settings(database_url="postgresql+psycopg://localhost:5432/masm")
    return TestClient(create_app(settings))


def test_health_is_public_and_returns_2xx() -> None:
    """Health 无需认证即可公开返回 2xx，且不泄露依赖机密。"""
    with _make_client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
