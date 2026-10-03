"""Add 幂等性集成测试。"""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import select

from masm.providers.embeddings import EmbeddingProvider
from masm.providers.errors import ProviderUnavailableError
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.schemas.api import AddRequest
from masm.services.add_service import AddConflictError, AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory
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
    assert len(commit.memory_ids) == 2


def test_dual_memory_replay_and_takeover(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """双消息 Add 回放不能追加消息记忆，也不能重新请求向量服务。"""
    provider = _SwitchableEmbeddings()
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings, embeddings=provider)
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[
            {"role": "user", "content": "Alice likes tea"},
            {"role": "assistant", "content": "She visited Kyoto"},
        ],
    )

    first = service.add(request)
    calls_after_first = provider.calls
    provider.available = False
    replay = service.add(request)

    with database.session() as session:
        rows = session.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.request_id == request_id)
        ).scalars().all()
    assert replay == first
    assert provider.calls == calls_after_first
    assert [(row.granularity, row.source_position) for row in rows] == [
        ("context", None),
        ("message", 0),
        ("message", 1),
    ]


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
    assert len(commit.memory_ids) == 2


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
    assert len(matches) == 2


class _SwitchableEmbeddings(EmbeddingProvider):
    """真实确定性向量委托，允许测试在首次提交后关闭 Provider。"""

    model_name = "text-embedding-v4"
    model_version = "cycle2-fixed"

    def __init__(self) -> None:
        self._delegate = DeterministicFakeEmbeddingProvider()
        self.dimensions = self._delegate.dimensions
        self.calls = 0
        self.available = True

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if not self.available:
            raise ProviderUnavailableError("provider is down")
        return self._delegate.embed_texts(texts)

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        self.calls += 1
        if not self.available:
            raise ProviderUnavailableError("provider is down")
        return self._delegate.embed_images(images)


def test_committed_replay_skips_llm_and_embedding_when_providers_are_down(
    database: Database, asset_store: AssetStore, settings
) -> None:
    provider = _SwitchableEmbeddings()
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=provider
    )
    request = _request(f"r-{_uid()}", f"u-{_uid()}")
    first = service.add(request)
    calls_after_commit = provider.calls
    provider.available = False

    replay = service.add(request)

    assert replay == first
    assert provider.calls == calls_after_commit
