"""Add 持久性集成测试。"""

import base64
import io
import threading
from collections.abc import Sequence
from uuid import uuid4

from PIL import Image
from sqlalchemy import func, select

from masm.providers.embeddings import EmbeddingProvider
from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, SourceMessage
from masm.storage.repositories import MemoryRepository


def _uid() -> str:
    return uuid4().hex[:12]


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (40, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url(data: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


def _count(database: Database, model: type, user_id: str) -> int:
    with database.session() as session:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )


class _FaultyProvider(EmbeddingProvider):
    """先正常工作，置位 unavailable 后固定抛错，并记录调用次数。"""

    model_name = "faulty-multimodal"
    model_version = "v1"

    def __init__(self, delegate: EmbeddingProvider) -> None:
        self._delegate = delegate
        self.dimensions = delegate.dimensions
        self.text_calls = 0
        self.image_calls = 0
        self.unavailable = False

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self._guard()
        self.text_calls += 1
        return self._delegate.embed_texts(texts)

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        self._guard()
        self.image_calls += 1
        return self._delegate.embed_images(images)

    def _guard(self) -> None:
        if self.unavailable:
            raise RuntimeError("embedding provider unavailable")


class _BlockingProvider(EmbeddingProvider):
    """在文本向量生成处阻塞，用于确定性构造「prepare 期间被抢先提交」的竞态。"""

    model_name = "blocking-multimodal"
    model_version = "v1"

    def __init__(
        self,
        delegate: EmbeddingProvider,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self._delegate = delegate
        self.dimensions = delegate.dimensions
        self._entered = entered
        self._release = release

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self._entered.set()
        self._release.wait(timeout=10)
        return self._delegate.embed_texts(texts)

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        return self._delegate.embed_images(images)


def _text_request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def test_added_memory_survives_restart(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """重启后（新服务实例）此前 Add 的数据仍然存在且可检索。"""
    user_id = f"u-{_uid()}"
    request_id = f"r-{_uid()}"
    request = _text_request(request_id, user_id, "durable content")

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


def test_committed_text_retry_skips_embedding_provider(
    database: Database, asset_store: AssetStore, settings, embeddings: EmbeddingProvider
) -> None:
    """COMMITTED 的文本请求重试直接回放：Provider 故障不影响结果，调用次数不增加。"""
    provider = _FaultyProvider(embeddings)
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=provider)
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    request = _text_request(request_id, user_id, "durable text memory")

    first = service.add(request)
    assert first.success is True
    calls_after_first = (provider.text_calls, provider.image_calls)
    assert calls_after_first == (1, 0)

    provider.unavailable = True
    retry = service.add(request)

    assert retry == first
    assert (provider.text_calls, provider.image_calls) == calls_after_first


def test_committed_image_retry_skips_embedding_provider(
    database: Database, asset_store: AssetStore, settings, embeddings: EmbeddingProvider
) -> None:
    """COMMITTED 的图片请求重试既不重新调用文本也不重新调用图片 Embedding。"""
    provider = _FaultyProvider(embeddings)
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=provider)
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "durable image memory"},
                    {"type": "image_url", "image_url": {"url": _data_url(_png())}},
                ],
            }
        ],
    )

    first = service.add(request)
    assert first.success is True
    calls_after_first = (provider.text_calls, provider.image_calls)
    assert calls_after_first == (1, 1)

    provider.unavailable = True
    retry = service.add(request)

    assert retry == first
    assert (provider.text_calls, provider.image_calls) == calls_after_first


def test_commit_during_prepare_is_replayed_without_duplicates(
    database: Database, asset_store: AssetStore, settings, embeddings: EmbeddingProvider
) -> None:
    """初次检查之后、prepare 期间其他处理者完成提交：回放原结果且不重复写入。"""
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    request = _text_request(request_id, user_id, "racy durable memory")

    entered = threading.Event()
    release = threading.Event()

    slow_service = AddService(
        MemoryRepository(database),
        asset_store,
        settings,
        embeddings=_BlockingProvider(embeddings, entered, release),
    )
    results: dict[str, object] = {}

    def _slow_add() -> None:
        results["slow"] = slow_service.add(request)

    thread = threading.Thread(target=_slow_add)
    thread.start()
    assert entered.wait(timeout=10), "慢处理者未能进入向量生成阶段"

    winner = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    results["winner"] = winner.add(request)

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert results["slow"] == results["winner"]
    assert getattr(results["winner"], "success", None) is True
    assert _count(database, Memory, user_id) == 1
    assert _count(database, SourceMessage, user_id) == 1
