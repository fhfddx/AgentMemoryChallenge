"""Add 持久性集成测试。"""

import base64
import io
import threading
from collections.abc import Sequence
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Asset, Memory, SourceMessage
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


class _BlockingFailingProvider(EmbeddingProvider):
    """在文本向量生成处阻塞，释放后固定抛错。

    用于确定性构造：A 阻塞在 Provider 阶段期间，B 用同一 request_id 完成提交，
    随后 A 的 Provider 报错。
    """

    model_name = "blocking-failing-multimodal"
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
        raise RuntimeError("embedding provider failed after concurrent commit")

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        raise RuntimeError("embedding provider failed after concurrent commit")


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


def test_image_mapping_failure_is_preclaim(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """图片向量数量不足时必须在 claim/资产写入之前失败。"""

    class ShortImageProvider(DeterministicFakeEmbeddingProvider):
        def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
            return super().embed_images(images[:1])

    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings, embeddings=ShortImageProvider())
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _data_url(_png())}},
                    {"type": "image_url", "image_url": {"url": _data_url(_png())}},
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="数量"):
        service.add(request)

    assert repo.get_ledger(user_id, request_id) is None
    assert _count(database, Memory, user_id) == 0
    assert _count(database, Asset, user_id) == 0


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
    assert _count(database, Memory, user_id) == 2
    assert _count(database, SourceMessage, user_id) == 1


def test_prepare_failure_after_concurrent_commit_replays(
    database: Database, asset_store: AssetStore, settings, embeddings: EmbeddingProvider
) -> None:
    """A 在 Provider 阶段阻塞期间 B 完成提交：A 的 Provider 报错必须回放成功结果。"""
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"
    request = _text_request(request_id, user_id, "concurrent durable memory")

    entered = threading.Event()
    release = threading.Event()
    slow_service = AddService(
        MemoryRepository(database),
        asset_store,
        settings,
        embeddings=_BlockingFailingProvider(embeddings, entered, release),
    )
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _slow_add() -> None:
        try:
            results["slow"] = slow_service.add(request)
        except BaseException as exc:  # noqa: BLE001 - 记录任何异常用于断言
            errors.append(exc)

    thread = threading.Thread(target=_slow_add)
    thread.start()
    assert entered.wait(timeout=10), "A 未能进入 Provider 调用"

    winner = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    results["winner"] = winner.add(request)

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    # A 绝不能暴露 Provider 异常，必须回放 B 的成功结果。
    assert errors == []
    assert getattr(results["slow"], "success", None) is True
    assert results["slow"] == results["winner"]

    assert _count(database, Memory, user_id) == 2
    assert _count(database, SourceMessage, user_id) == 1
    assert MemoryRepository(database).get_ledger_status(user_id, request_id) == "COMMITTED"


def test_provider_failure_leaves_no_ledger_or_writes(
    database: Database, asset_store: AssetStore, settings, embeddings: EmbeddingProvider
) -> None:
    """无并发提交时 Provider 失败：不得遗留 PROCESSING，也不得产生任何写入。"""
    provider = _FaultyProvider(embeddings)
    provider.unavailable = True
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=provider)
    user_id, request_id = f"u-{_uid()}", f"r-{_uid()}"

    with pytest.raises(RuntimeError):
        service.add(_text_request(request_id, user_id, "failing provider memory"))

    repo = MemoryRepository(database)
    assert repo.get_ledger(user_id, request_id) is None
    assert _count(database, Memory, user_id) == 0
    assert _count(database, SourceMessage, user_id) == 0
