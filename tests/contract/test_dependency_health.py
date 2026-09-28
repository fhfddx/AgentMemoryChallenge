"""部署健康检查契约测试。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from masm.api.app import create_app
from masm.api.health import probe_dependencies
from masm.config import Settings


class _BrokenEngine:
    def connect(self):
        raise RuntimeError("postgresql://admin:secret@private.example/masm")


class _BrokenDatabase:
    engine = _BrokenEngine()


class _BrokenAssetStore:
    base_dir = Path("Z:/private/secret-bucket")

    def check_ready(self) -> None:
        raise OSError("s3://private-bucket/access-key")


def test_public_health_stays_shallow_when_dependencies_are_unavailable() -> None:
    """公开 Health 不连接依赖，也不要求认证。"""
    settings = Settings(database_url="postgresql+psycopg://unused/masm")
    application = create_app(
        settings,
        database=_BrokenDatabase(),  # type: ignore[arg-type]
        asset_store=_BrokenAssetStore(),  # type: ignore[arg-type]
    )
    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_internal_dependency_probe_returns_only_sanitized_states() -> None:
    """内部探针只返回固定状态，不泄露异常、连接串、路径或密钥。"""
    result = probe_dependencies(_BrokenDatabase(), _BrokenAssetStore())  # type: ignore[arg-type]
    serialized = json.dumps(result, sort_keys=True)

    assert result == {
        "status": "degraded",
        "dependencies": {"asset_store": "unavailable", "database": "unavailable"},
    }
    for forbidden in ("secret", "private", "postgresql", "s3://", "Z:"):
        assert forbidden not in serialized
