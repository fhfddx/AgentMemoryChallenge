"""冲突感知重排单元测试（只产生相关性/排序/证据元数据，不生成答案正文）。"""

from uuid import UUID, uuid4

from masm.providers.reranker import LexicalReranker, RerankerProvider
from masm.retrieval.baseline import ParsedQuery
from masm.retrieval.reranker import EvidenceReranker, RankedEvidence
from masm.storage.types import MemoryCandidate

_USER = "user-1"


def _candidate(
    content: str, score: float = 0.0, **overrides: object
) -> MemoryCandidate:
    payload: dict = {
        "memory_id": uuid4(),
        "user_id": _USER,
        "content": content,
        "score": score,
    }
    payload.update(overrides)
    return MemoryCandidate(**payload)


def test_ranked_evidence_carries_only_relevance_metadata() -> None:
    """RankedEvidence 不得携带任何答案正文字段。"""
    fields = set(RankedEvidence.__dataclass_fields__)

    assert "content" in fields
    assert not {"answer", "response", "reply", "generated_text"} & fields


def test_result_content_is_never_rewritten() -> None:
    candidate = _candidate("the cat sat on the mat", score=1.0)
    reranker = EvidenceReranker(provider=LexicalReranker())

    ranked = reranker.rank(ParsedQuery(text_queries=("cat",)), [candidate])

    assert ranked[0].content == candidate.content
    assert ranked[0].memory_id == candidate.memory_id


def test_higher_relevance_ranks_first() -> None:
    low = _candidate("unrelated", score=0.1)
    high = _candidate("very relevant", score=0.9)
    reranker = EvidenceReranker()

    ranked = reranker.rank(ParsedQuery(text_queries=("relevant",)), [low, high])

    assert [item.memory_id for item in ranked] == [high.memory_id, low.memory_id]


def test_conflict_peers_are_kept_together_and_marked() -> None:
    group = uuid4()
    first = _candidate("meeting on Monday", score=0.5, conflict_group_id=group)
    second = _candidate("meeting on Tuesday", score=0.4, conflict_group_id=group)
    other = _candidate("unrelated memory", score=0.45)
    reranker = EvidenceReranker()

    ranked = reranker.rank(
        ParsedQuery(text_queries=("meeting",)), [first, other, second]
    )

    by_id = {item.memory_id: item for item in ranked}
    assert by_id[first.memory_id].conflict_group_id == group
    assert by_id[second.memory_id].conflict_group_id == group
    assert by_id[second.memory_id].is_conflict_peer is True
    # 冲突双方保留冲突身份且都进入结果。
    assert {first.memory_id, second.memory_id} <= {item.memory_id for item in ranked}


def test_duplicate_candidates_are_penalised() -> None:
    original = _candidate("the same memory", score=0.6)
    duplicate = _candidate("the same memory", score=0.6, duplicate_of=original.memory_id)
    reranker = EvidenceReranker()

    ranked = reranker.rank(ParsedQuery(text_queries=("same",)), [duplicate, original])

    assert ranked[0].memory_id == original.memory_id
    assert ranked[1].duplicate_of == original.memory_id


def test_entity_and_time_match_increase_score() -> None:
    matching = _candidate("alice at the office", score=0.5)
    other = _candidate("bob at the park", score=0.5)
    reranker = EvidenceReranker()
    query = ParsedQuery(
        text_queries=("alice",), entities=("alice",), location_constraints=("office",)
    )

    ranked = reranker.rank(query, [other, matching])

    assert ranked[0].memory_id == matching.memory_id
    assert ranked[0].matched_entities == ("alice",)


def test_reranker_provider_cannot_return_free_text() -> None:
    """Provider 只能返回分数：返回字符串列表必须被拒绝。"""

    class _TextProvider(RerankerProvider):
        model_name = "text-provider"

        def score(self, query: str, documents) -> list[float]:
            return ["the answer is 42"] * len(documents)  # type: ignore[list-item]

    reranker = EvidenceReranker(provider=_TextProvider())

    ranked = reranker.rank(ParsedQuery(text_queries=("x",)), [_candidate("doc")])

    # 非法分数被忽略，仍然得到确定性的规则排序。
    assert ranked[0].content == "doc"


def test_reranker_provider_failure_degrades_to_rules() -> None:
    class _BrokenProvider(RerankerProvider):
        model_name = "broken"

        def score(self, query: str, documents) -> list[float]:
            raise RuntimeError("provider down")

    candidate = _candidate("doc", score=0.5)
    reranker = EvidenceReranker(provider=_BrokenProvider())

    ranked = reranker.rank(ParsedQuery(text_queries=("doc",)), [candidate])

    assert ranked[0].memory_id == candidate.memory_id


def test_ranked_evidence_records_channels() -> None:
    candidate = _candidate("doc", score=0.5)
    reranker = EvidenceReranker()

    ranked = reranker.rank(ParsedQuery(text_queries=("doc",)), [candidate])

    assert ranked[0].rank == 1
    assert isinstance(ranked[0].score, float)


def test_empty_candidates_return_empty_list() -> None:
    assert EvidenceReranker().rank(ParsedQuery(text_queries=("x",)), []) == []


def test_hard_cap_is_enforced() -> None:
    from masm.retrieval.reranker import MAX_RERANK_CANDIDATES

    candidates = [_candidate(f"doc {index}", score=index / 100) for index in range(200)]

    ranked = EvidenceReranker().rank(ParsedQuery(text_queries=("doc",)), candidates)

    assert len(ranked) <= MAX_RERANK_CANDIDATES


def test_high_score_candidate_at_input_tail_is_admitted() -> None:
    """无冲突时不得按输入顺序丢弃尾部的高分候选。"""
    from masm.retrieval.reranker import MAX_RERANK_CANDIDATES

    filler = [_candidate(f"filler {index}", score=0.1) for index in range(MAX_RERANK_CANDIDATES)]
    best = _candidate("best evidence", score=0.99)

    ranked = EvidenceReranker().rank(ParsedQuery(text_queries=("best",)), [*filler, best])

    assert ranked[0].memory_id == best.memory_id
    assert len(ranked) <= MAX_RERANK_CANDIDATES


def test_conflict_peers_survive_candidate_admission() -> None:
    """初始候选占满硬上限、冲突同伴位于输入尾部时仍必须进入重排池。"""
    from masm.retrieval.reranker import MAX_RERANK_CANDIDATES

    group = uuid4()
    filler = [_candidate(f"filler {index}", score=0.5) for index in range(MAX_RERANK_CANDIDATES)]
    first = _candidate("conflict one", score=0.2, conflict_group_id=group)
    peer = _candidate("conflict two", score=0.1, conflict_group_id=group)

    ranked = EvidenceReranker().rank(
        ParsedQuery(text_queries=("conflict",)), [*filler, first, peer]
    )

    ids = {item.memory_id for item in ranked}
    assert {first.memory_id, peer.memory_id} <= ids
    assert len(ranked) <= MAX_RERANK_CANDIDATES


def test_isolation_of_ids_in_ranked_evidence() -> None:
    candidate = _candidate("doc", score=0.5)
    ranked = EvidenceReranker().rank(ParsedQuery(text_queries=("doc",)), [candidate])

    assert isinstance(ranked[0].memory_id, UUID)
    assert ranked[0].user_id == _USER


def test_message_precedes_same_run_context() -> None:
    """同一 Add 的精确消息证据优先于冗长的父上下文。"""
    context = _candidate("needle " + "irrelevant " * 200, score=0.6, request_id="run-1")
    message = _candidate(
        "needle fact", score=0.6, granularity="message",
        request_id="run-1", source_position=0,
    )

    ranked = EvidenceReranker().rank(
        ParsedQuery(text_queries=("needle",)), [context, message]
    )

    assert [item.memory_id for item in ranked] == [message.memory_id, context.memory_id]


def test_message_granularity_does_not_overpower_higher_relevance() -> None:
    context = _candidate("relevant context", score=0.60, request_id="run-1")
    message = _candidate(
        "weaker message", score=0.59, granularity="message",
        request_id="run-1", source_position=0,
    )

    ranked = EvidenceReranker().rank(
        ParsedQuery(text_queries=("query",)), [message, context]
    )

    assert [item.memory_id for item in ranked] == [context.memory_id, message.memory_id]
    assert [item.score for item in ranked] == [0.60, 0.59]


def test_ranked_metadata_preserves_raw_retrieval_signals() -> None:
    candidate = _candidate(
        "matched", score=0.5,
        retrieval_signals={"lexical": 0.2, "text_vector": 0.7},
    )

    ranked = EvidenceReranker().rank(ParsedQuery(text_queries=("matched",)), [candidate])

    assert ranked[0].metadata == {
        "base_score": 0.5,
        "lexical": 0.2,
        "text_vector": 0.7,
    }


def test_top_100_prefers_distinct_message_positions_over_same_run_parent() -> None:
    context = _candidate(
        "needle parent", score=0.5, memory_id=UUID(int=0), request_id="run-1"
    )
    messages = [
        _candidate(
            f"needle fact {index}", score=0.5, memory_id=UUID(int=index + 1),
            granularity="message", request_id="run-1", source_position=index,
        )
        for index in range(100)
    ]

    ranked = EvidenceReranker().rank(
        ParsedQuery(text_queries=("needle",)), [context, *messages]
    )

    assert {item.memory_id for item in ranked} == {item.memory_id for item in messages}


def test_top_100_with_provider_input_capped_at_64() -> None:
    class _RecordingProvider(RerankerProvider):
        model_name = "recording"

        def __init__(self) -> None:
            self.input_count = 0

        def score(self, query: str, documents) -> list[float]:
            self.input_count = len(documents)
            return [0.0] * len(documents)

    provider = _RecordingProvider()
    candidates = [_candidate(f"fact {index}", score=1 - index / 1000) for index in range(120)]

    ranked = EvidenceReranker(provider=provider).rank(
        ParsedQuery(text_queries=("fact",)), candidates
    )

    assert len(ranked) == 100
    assert provider.input_count <= 64
