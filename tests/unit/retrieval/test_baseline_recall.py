"""基线检索并行召回与确定性通道顺序的单元测试。

并行性用 ``threading.Barrier`` 证明：只有当所有通道的召回调用同时在场时屏障才会放行，
因此不依赖任何 sleep 计时。
"""

import threading
from uuid import UUID, uuid4

from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.retrieval.baseline import (
    IMAGE_VECTOR_CHANNEL,
    LEXICAL_CHANNEL,
    METADATA_CHANNEL,
    TEXT_VECTOR_CHANNEL,
    BaselineRetriever,
    ParsedQuery,
)
from masm.storage.types import MemoryCandidate

_WEIGHTS = {
    LEXICAL_CHANNEL: 1.0,
    TEXT_VECTOR_CHANNEL: 1.0,
    IMAGE_VECTOR_CHANNEL: 0.8,
    METADATA_CHANNEL: 0.4,
}

_BARRIER_TIMEOUT = 5.0


def _candidate(content: str, user_id: str = "user-1") -> MemoryCandidate:
    return MemoryCandidate(memory_id=uuid4(), user_id=user_id, content=content, score=0.0)


def _mixed_query() -> ParsedQuery:
    """文本 + 图片查询会同时启用全文、文本向量、图片向量与元数据四个通道。"""
    return ParsedQuery(
        text_queries=("hello world",),
        visual_queries=(b"image-bytes",),
        intent="visual",
    )


class _BarrierRepository:
    """四个通道的召回调用必须同时抵达屏障，否则 Barrier 超时并让测试失败。"""

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=_BARRIER_TIMEOUT)
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _enter(self, channel: str) -> None:
        with self._lock:
            self.calls.append(channel)
        self.barrier.wait()

    def lexical_candidates(self, user_id: str, query: str, limit: int) -> list[MemoryCandidate]:
        self._enter(LEXICAL_CHANNEL)
        return [_candidate("lexical")]

    def vector_candidates(
        self,
        user_id: str,
        vector,
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        self._enter(f"{modality}_vector")
        return [_candidate(f"{modality}-vector")]

    def metadata_candidates(
        self, user_id: str, *, modality=None, keywords=(), limit: int
    ) -> list[MemoryCandidate]:
        self._enter(METADATA_CHANNEL)
        return [_candidate("metadata")]


class _InvertedRepository:
    """让最先提交的全文通道最后返回，用于验证收集顺序与完成顺序无关。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vector_calls = 0
        self.vectors_done = threading.Event()

    def lexical_candidates(self, user_id: str, query: str, limit: int) -> list[MemoryCandidate]:
        if not self.vectors_done.wait(timeout=_BARRIER_TIMEOUT):
            raise AssertionError("向量通道未先完成：召回没有并行执行")
        return [_candidate("lexical")]

    def vector_candidates(
        self,
        user_id: str,
        vector,
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        with self._lock:
            self._vector_calls += 1
            if self._vector_calls >= 2:
                self.vectors_done.set()
        return [_candidate(f"{modality}-vector")]

    def metadata_candidates(
        self, user_id: str, *, modality=None, keywords=(), limit: int
    ) -> list[MemoryCandidate]:
        return [_candidate("metadata")]


def test_channel_recalls_actually_overlap() -> None:
    """四个独立召回通道的调用必须实际重叠。"""
    repository = _BarrierRepository(parties=4)
    retriever = BaselineRetriever(
        repository, DeterministicFakeEmbeddingProvider(), _WEIGHTS
    )

    results = retriever.retrieve("user-1", _mixed_query(), 5)

    assert sorted(repository.calls) == [
        IMAGE_VECTOR_CHANNEL,
        LEXICAL_CHANNEL,
        METADATA_CHANNEL,
        "text_vector",
    ]
    assert len(results) == 4


def test_channel_collection_order_follows_submission_order() -> None:
    """先提交的通道即使最后返回，收集顺序仍固定为提交顺序。"""
    repository = _InvertedRepository()
    retriever = BaselineRetriever(
        repository, DeterministicFakeEmbeddingProvider(), _WEIGHTS
    )

    channels, _catalogue = retriever._recall("user-1", _mixed_query(), 5)

    assert [channel.name for channel in channels] == [
        LEXICAL_CHANNEL,
        TEXT_VECTOR_CHANNEL,
        IMAGE_VECTOR_CHANNEL,
        METADATA_CHANNEL,
    ]


class _FixedRepository:
    """每个通道返回固定候选，用于验证融合结果可复现。"""

    def __init__(self) -> None:
        self._by_channel = {
            LEXICAL_CHANNEL: _fixed(UUID(int=1), "lexical"),
            "text_vector": _fixed(UUID(int=2), "text"),
            "image_vector": _fixed(UUID(int=3), "image"),
            METADATA_CHANNEL: _fixed(UUID(int=4), "metadata"),
        }

    def lexical_candidates(self, user_id: str, query: str, limit: int) -> list[MemoryCandidate]:
        return [self._by_channel[LEXICAL_CHANNEL]]

    def vector_candidates(
        self,
        user_id: str,
        vector,
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        return [self._by_channel[f"{modality}_vector"]]

    def metadata_candidates(
        self, user_id: str, *, modality=None, keywords=(), limit: int
    ) -> list[MemoryCandidate]:
        return [self._by_channel[METADATA_CHANNEL]]


def _fixed(memory_id: UUID, content: str) -> MemoryCandidate:
    return MemoryCandidate(memory_id=memory_id, user_id="user-1", content=content, score=0.0)


def test_fused_result_is_deterministic_across_runs() -> None:
    """相同输入多次检索得到完全一致的顺序与分数。"""
    query = _mixed_query()
    runs = [
        BaselineRetriever(_FixedRepository(), DeterministicFakeEmbeddingProvider(), _WEIGHTS)
        .retrieve("user-1", query, 5)
        for _ in range(3)
    ]

    expected = [(candidate.content, candidate.score) for candidate in runs[0]]
    assert expected
    for run in runs[1:]:
        assert [(candidate.content, candidate.score) for candidate in run] == expected


class _RecordingRepository:
    """记录向量召回模态，验证正式共享空间的跨模态查询。"""

    def __init__(self) -> None:
        self.modalities: list[str] = []

    def lexical_candidates(self, user_id: str, query: str, limit: int) -> list[MemoryCandidate]:
        return []

    def vector_candidates(
        self,
        user_id: str,
        vector,
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        self.modalities.append(modality)
        return [_candidate("image memory")] if modality == "image" else []

    def metadata_candidates(
        self, user_id: str, *, modality=None, keywords=(), limit: int
    ) -> list[MemoryCandidate]:
        return []


def test_shared_embedding_space_uses_text_query_for_text_and_image_memories() -> None:
    """正式共享向量空间中，文本查询必须同时召回图片描述向量。"""
    repository = _RecordingRepository()
    retriever = BaselineRetriever(
        repository,
        DeterministicFakeEmbeddingProvider(),
        _WEIGHTS,
        text_queries_search_images=True,
    )

    results = retriever.retrieve("user-1", ParsedQuery(text_queries=("purple square",)), 5)

    assert sorted(repository.modalities) == ["image", "text"]
    assert [candidate.content for candidate in results] == ["image memory"]


def test_default_embedding_space_keeps_text_and_image_modalities_isolated() -> None:
    """默认 Fake 档位保持旧的模态隔离行为。"""
    repository = _RecordingRepository()
    retriever = BaselineRetriever(
        repository, DeterministicFakeEmbeddingProvider(), _WEIGHTS
    )

    retriever.retrieve("user-1", ParsedQuery(text_queries=("purple square",)), 5)

    assert repository.modalities == ["text"]


def test_channels_preserve_provenance() -> None:
    """四个通道经 RRF 融合后都必须保留原始来源定位。"""
    repository = _FixedRepository()
    expected = {
        LEXICAL_CHANNEL: ("message", "lex-run", 0),
        TEXT_VECTOR_CHANNEL: ("message", "text-run", 1),
        IMAGE_VECTOR_CHANNEL: ("message", "image-run", 2),
        METADATA_CHANNEL: ("context", "meta-run", None),
    }
    for channel, candidate in repository._by_channel.items():
        granularity, request_id, source_position = expected[channel]
        repository._by_channel[channel] = MemoryCandidate(
            memory_id=candidate.memory_id,
            user_id=candidate.user_id,
            content=candidate.content,
            score=candidate.score,
            granularity=granularity,
            request_id=request_id,
            source_position=source_position,
        )

    found = BaselineRetriever(
        repository, DeterministicFakeEmbeddingProvider(), _WEIGHTS
    ).retrieve("user-1", _mixed_query(), 10)

    assert {
        row.content: (row.granularity, row.request_id, row.source_position)
        for row in found
    } == {
        "lexical": expected[LEXICAL_CHANNEL],
        "text": expected[TEXT_VECTOR_CHANNEL],
        "image": expected[IMAGE_VECTOR_CHANNEL],
        "metadata": expected[METADATA_CHANNEL],
    }
