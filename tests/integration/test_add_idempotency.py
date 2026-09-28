"""Add 幂等性集成测试。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from masm.schemas.api import AddRequest
from masm.services.add_service import AddConflictError, AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


def _request(request_id: str, user_id: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": "hello memory"}],
    )


def _uid() -> str:
    return uuid4().hex[:12]


def test_same_request_id_writes_once(add_service: AddService, database: Database) -> None:
    """相同 user_id + request_id 只产生一次逻辑写入。"""
    repo = MemoryRepository(database)
    user_id = f"u-{_uid()}"
    request_id = f"r-{_uid()}"
    request = _request(request_id, user_id)

    first = add_service.add(request)
    second = add_service.add(request)

    assert first == second
    commit = repo.get_by_request(user_id, request_id)
    assert commit is not None
    assert len(commit.memory_ids) == 1


def test_restart_keeps_idempotency(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """应用重启后相同 request_id 仍然幂等。"""
    user_id = f"u-{_uid()}"
    request_id = f"r-{_uid()}"
    request = _request(request_id, user_id)

    service_one = AddService(MemoryRepository(database), asset_store, settings)
    service_one.add(request)

    # 模拟重启：新 Repository 与 AssetStore，指向同一数据库与目录。
    service_two = AddService(MemoryRepository(database), asset_store, settings)
    service_two.add(request)

    commit = MemoryRepository(database).get_by_request(user_id, request_id)
    assert commit is not None
    assert len(commit.memory_ids) == 1


def test_concurrent_same_request_writes_once(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """并发相同请求不会重复写入。"""
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings)
    user_id = f"u-{_uid()}"
    request = _request(f"r-{_uid()}", user_id)

    def _attempt() -> str:
        try:
            service.add(request)
            return "ok"
        except AddConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: _attempt(), range(4)))

    matches = repo.lexical_candidates(user_id, "memory", limit=100)
    assert len(matches) == 1
