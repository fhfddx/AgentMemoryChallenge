"""Bounded multi-view history candidate construction tests."""

from uuid import uuid4

from masm.orchestration.history_candidates import (
    MAX_TEXT_QUERIES,
    RECENT_SLOTS,
    SEMANTIC_SLOTS,
    HistoryCandidateBuilder,
)
from masm.retrieval.baseline import ParsedQuery
from masm.schemas.agents import Entity, EntityKind, PerceptionResult
from masm.schemas.api import AddRequest
from masm.storage.types import MemoryCandidate


def _request(user_id: str = "user-1", text: str = "Root links the bridge.") -> AddRequest:
    return AddRequest(
        request_id="request-1",
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _perception() -> PerceptionResult:
    return PerceptionResult(
        entities=(
            Entity(name="Root", kind=EntityKind.OTHER, confidence=0.9, evidence="Root"),
            Entity(name="Bridge", kind=EntityKind.OTHER, confidence=0.9, evidence="bridge"),
        ),
        keywords=("bridge", "Terminal"),
    )


def _candidate(
    label: str,
    *,
    user_id: str = "user-1",
    granularity: str = "context",
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=uuid4(),
        user_id=user_id,
        content=label,
        score=1.0,
        granularity=granularity,  # type: ignore[arg-type]
    )


class _FakeRepository:
    def __init__(
        self, recent: list[MemoryCandidate] | None = None, *, has_context: bool = True
    ) -> None:
        self.recent = recent or []
        self.has_context = has_context
        self.has_context_calls: list[str] = []
        self.metadata_calls: list[dict[str, object]] = []

    def has_context_memory(self, user_id: str) -> bool:
        self.has_context_calls.append(user_id)
        return self.has_context

    def metadata_candidates(self, user_id: str, **kwargs: object) -> list[MemoryCandidate]:
        self.metadata_calls.append({"user_id": user_id, **kwargs})
        return list(self.recent)


class _FakeRetriever:
    def __init__(
        self,
        semantic: list[MemoryCandidate] | None = None,
        recent: list[MemoryCandidate] | None = None,
        *,
        has_context: bool = True,
    ) -> None:
        self.repository = _FakeRepository(recent, has_context=has_context)
        self.semantic = semantic or []
        self.calls: list[tuple[str, ParsedQuery, int, str | None]] = []

    def retrieve(
        self, user_id: str, query: ParsedQuery, limit: int, *, granularity: str | None = None
    ) -> list[MemoryCandidate]:
        self.calls.append((user_id, query, limit, granularity))
        return list(self.semantic)


def _builder(retriever: _FakeRetriever, *, max_history: int = 8) -> HistoryCandidateBuilder:
    return HistoryCandidateBuilder(retriever, max_history=max_history)  # type: ignore[arg-type]


def test_builds_stable_raw_entity_keyword_and_combined_queries() -> None:
    retriever = _FakeRetriever()

    _builder(retriever).build(_request(), _perception())

    query = retriever.calls[0][1]
    assert tuple(query.text_queries) == (
        "Root links the bridge.",
        "Root Bridge",
        "bridge Terminal",
        "Root Bridge Terminal",
    )


def test_batches_at_most_four_text_variants_and_supplies_metadata_entities() -> None:
    retriever = _FakeRetriever()

    _builder(retriever).build(_request(), _perception())

    assert len(retriever.calls) == 1
    query = retriever.calls[0][1]
    assert len(query.text_queries) == MAX_TEXT_QUERIES == 4
    assert tuple(query.entities) == ("Root", "Bridge", "Terminal")
    assert retriever.calls[0][2:] == (8, "context")


def test_reserves_two_recent_contexts_and_deduplicates_semantic_overlap() -> None:
    semantic = [_candidate(f"semantic-{index}") for index in range(8)]
    recent_unique = _candidate("recent-unique")
    retriever = _FakeRetriever(
        semantic,
        [semantic[0], recent_unique, semantic[1]],
    )

    result = _builder(retriever).build(_request(), _perception())

    assert SEMANTIC_SLOTS == 6
    assert RECENT_SLOTS == 2
    assert [item.memory_id for item in result] == [
        *(item.memory_id for item in semantic[:6]),
        recent_unique.memory_id,
        semantic[6].memory_id,
    ]


def test_unused_recent_slots_return_to_semantic_candidates_in_order() -> None:
    semantic = [_candidate(f"semantic-{index}") for index in range(8)]
    retriever = _FakeRetriever(semantic, [semantic[0], semantic[1]])

    result = _builder(retriever).build(_request(), _perception())

    assert [item.memory_id for item in result] == [item.memory_id for item in semantic]


def test_history_never_exceeds_eight_and_contains_only_same_user_contexts() -> None:
    valid = [_candidate(f"valid-{index}") for index in range(10)]
    semantic = [
        _candidate("foreign", user_id="user-2"),
        _candidate("message", granularity="message"),
        *valid,
    ]
    retriever = _FakeRetriever(semantic, [_candidate("recent"), *valid])

    result = _builder(retriever).build(_request(), _perception())

    assert len(result) == 8
    assert len({item.memory_id for item in result}) == 8
    assert all(item.user_id == "user-1" and item.granularity == "context" for item in result)


def test_zero_history_and_user_without_context_skip_embeddings_and_recent_query() -> None:
    zero = _FakeRetriever(has_context=True)
    missing = _FakeRetriever(has_context=False)

    assert _builder(zero, max_history=0).build(_request(), _perception()) == []
    assert _builder(missing).build(_request(), _perception()) == []
    assert zero.calls == []
    assert zero.repository.metadata_calls == []
    assert missing.calls == []
    assert missing.repository.metadata_calls == []


def test_other_user_history_does_not_trigger_recall() -> None:
    retriever = _FakeRetriever(
        semantic=[_candidate("other", user_id="user-2")],
        recent=[_candidate("other-recent", user_id="user-2")],
        has_context=False,
    )

    assert _builder(retriever).build(_request(user_id="user-1"), _perception()) == []
    assert retriever.repository.has_context_calls == ["user-1"]
    assert retriever.calls == []
