"""Add 持久性集成测试。"""

from uuid import uuid4

from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


def _uid() -> str:
    return uuid4().hex[:12]


def test_added_memory_survives_restart(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """重启后（新服务实例）此前 Add 的数据仍然存在且可检索。"""
    user_id = f"u-{_uid()}"
    request_id = f"r-{_uid()}"
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": "durable content"}],
    )

    service_one = AddService(MemoryRepository(database), asset_store, settings)
    response = service_one.add(request)
    assert response.success is True

    # 模拟重启：全新的 Repository 与 AssetStore。
    repo_two = MemoryRepository(database)
    commit = repo_two.get_by_request(user_id, request_id)
    assert commit is not None
    assert commit.request_id == request_id

    candidates = repo_two.lexical_candidates(user_id, "durable", limit=10)
    assert any("durable" in c.content for c in candidates)
