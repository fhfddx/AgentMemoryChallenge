"""Candidate-pool diversity without displacing several strong facts from one source."""

from uuid import UUID

import pytest

from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import build_selector_pool


def _evidence(number: int, request_id: str) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id="user-1",
        content=f"fact {number}",
        score=1.0 / number,
        rank=number,
        request_id=request_id,
    )


def test_pool_reserves_distinct_sources_without_losing_strong_same_source_facts() -> None:
    ranked = [
        *[_evidence(number, "strong-source") for number in range(1, 9)],
        *[_evidence(number, f"other-{number}") for number in range(9, 49)],
    ]

    pool = build_selector_pool(ranked)

    assert len(pool) == 32
    assert [item.rank for item in pool] == sorted(item.rank for item in pool)
    assert sum(item.request_id == "strong-source" for item in pool) >= 2
    assert len({item.request_id for item in pool}) >= 2


def test_pool_ignores_blank_source_during_reservation_and_deduplicates_ids() -> None:
    ranked = [
        _evidence(1, ""),
        _evidence(2, "alpha"),
        _evidence(2, "duplicate"),
        _evidence(3, "alpha"),
        _evidence(4, "alpha"),
        _evidence(5, "beta"),
    ]

    pool = build_selector_pool(ranked, max_candidates=4)

    assert [item.memory_id for item in pool] == [
        UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=5)
    ]


def test_pool_respects_zero_small_and_global_maximum() -> None:
    ranked = [_evidence(number, f"source-{number}") for number in range(1, 41)]

    assert build_selector_pool([]) == []
    assert build_selector_pool(ranked, max_candidates=0) == []
    assert len(build_selector_pool(ranked, max_candidates=2)) == 2
    assert len(build_selector_pool(ranked, max_candidates=100)) == 32


def test_pool_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        build_selector_pool([], max_candidates=-1)
