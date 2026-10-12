"""Bounded cross-request semantic-neighbor expansion tests."""

from uuid import UUID

from masm.retrieval.baseline import TEXT_VECTOR_CHANNEL
from masm.retrieval.semantic_expander import (
    MAX_NEIGHBORS,
    MAX_NEIGHBORS_PER_SEED,
    MAX_SEEDS,
    SemanticNeighborExpander,
)
from masm.storage.types import MemoryCandidate

_USER = "user-1"


def _candidate(
    number: int,
    *,
    request_id: str,
    score: float = 0.9,
    user_id: str = _USER,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=UUID(int=number),
        user_id=user_id,
        content=f"memory {number}",
        score=score,
        request_id=request_id,
    )


class _VectorRepository:
    def __init__(self) -> None:
        self.vectors: dict[UUID, tuple[float, ...]] = {}
        self.neighbors: dict[tuple[float, ...], list[MemoryCandidate]] = {}
        self.vector_reads: list[tuple[str, list[UUID], str, str]] = []
        self.candidate_calls: list[tuple[str, tuple[float, ...], tuple[str, ...], int]] = []

    def text_embedding_vectors(
        self, user_id: str, memory_ids, *, model_name: str, model_version: str
    ) -> dict[UUID, tuple[float, ...]]:
        ids = list(memory_ids)
        self.vector_reads.append((user_id, ids, model_name, model_version))
        return {
            memory_id: self.vectors[memory_id]
            for memory_id in ids
            if memory_id in self.vectors
        }

    def vector_candidates(
        self,
        user_id: str,
        vector,
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
        exclude_request_ids=(),
    ) -> list[MemoryCandidate]:
        assert modality == "text"
        assert model_name == "embedding-model"
        assert model_version == "embedding-v1"
        key = tuple(vector)
        excluded = tuple(exclude_request_ids)
        self.candidate_calls.append((user_id, key, excluded, limit))
        return list(self.neighbors.get(key, ()))[:limit]


def _expander(repository: _VectorRepository, **kwargs) -> SemanticNeighborExpander:
    return SemanticNeighborExpander(
        repository,
        model_name="embedding-model",
        model_version="embedding-v1",
        **kwargs,
    )


def test_seed_selection_is_distinct_request_ordered_and_hard_capped() -> None:
    repository = _VectorRepository()
    seeds = [_candidate(1, request_id="run-a"), _candidate(2, request_id="run-a")]
    seeds.extend(_candidate(number, request_id=f"run-{number}") for number in range(3, 9))
    for index, seed in enumerate(seeds, start=1):
        repository.vectors[seed.memory_id] = (float(index), 0.0)

    _expander(repository).expand(_USER, seeds)

    selected_ids = repository.vector_reads[0][1]
    assert selected_ids == [UUID(int=1), UUID(int=3), UUID(int=4), UUID(int=5)]
    assert len(repository.candidate_calls) == MAX_SEEDS
    assert all(
        call[2] == ("run-a", "run-3", "run-4", "run-5")
        for call in repository.candidate_calls
    )
    assert all(call[3] == MAX_NEIGHBORS_PER_SEED for call in repository.candidate_calls)


def test_missing_seed_vectors_are_a_no_op() -> None:
    repository = _VectorRepository()

    expanded = _expander(repository).expand(_USER, [_candidate(1, request_id="run-a")])

    assert expanded == []
    assert repository.candidate_calls == []


def test_filters_cross_user_seed_request_existing_and_low_similarity() -> None:
    repository = _VectorRepository()
    seed = _candidate(1, request_id="seed-run")
    repository.vectors[seed.memory_id] = (1.0, 0.0)
    repository.neighbors[(1.0, 0.0)] = [
        _candidate(2, request_id="good-run", score=0.70),
        _candidate(3, request_id="seed-run", score=0.99),
        _candidate(4, request_id="other-run", score=0.99, user_id="other-user"),
        _candidate(5, request_id="low-run", score=0.47),
        _candidate(6, request_id="known-run", score=0.95),
    ]

    expanded = _expander(repository, min_similarity=0.48).expand(
        _USER, [seed], known_ids={UUID(int=6)}
    )

    assert [candidate.memory_id for candidate in expanded] == [UUID(int=2)]


def test_deduplicates_stably_keeps_best_score_and_sets_vector_signal() -> None:
    repository = _VectorRepository()
    first_seed = _candidate(1, request_id="seed-a")
    second_seed = _candidate(2, request_id="seed-b")
    repository.vectors = {
        first_seed.memory_id: (1.0, 0.0),
        second_seed.memory_id: (0.0, 1.0),
    }
    repository.neighbors[(1.0, 0.0)] = [
        _candidate(10, request_id="neighbor-a", score=0.60),
        _candidate(11, request_id="neighbor-b", score=0.80),
    ]
    repository.neighbors[(0.0, 1.0)] = [
        _candidate(10, request_id="neighbor-a", score=0.95),
        _candidate(12, request_id="neighbor-c", score=0.75),
    ]

    expanded = _expander(repository).expand(_USER, [first_seed, second_seed])

    assert [candidate.memory_id for candidate in expanded] == [
        UUID(int=10), UUID(int=11), UUID(int=12)
    ]
    assert expanded[0].score == 0.855
    assert expanded[0].retrieval_signals == {TEXT_VECTOR_CHANNEL: 0.95}


def test_expansion_score_is_discounted_to_the_seed_rrf_scale() -> None:
    """原始余弦分数不能直接压过数量级更小的直召回 RRF 分数。"""
    repository = _VectorRepository()
    seed = _candidate(1, request_id="seed", score=0.04)
    repository.vectors[seed.memory_id] = (1.0, 0.0)
    repository.neighbors[(1.0, 0.0)] = [
        _candidate(2, request_id="neighbor", score=0.90)
    ]

    expanded = _expander(repository).expand(_USER, [seed])

    assert expanded[0].score == 0.036
    assert expanded[0].retrieval_signals == {TEXT_VECTOR_CHANNEL: 0.90}


def test_total_expansion_is_hard_capped() -> None:
    repository = _VectorRepository()
    seeds = [_candidate(number, request_id=f"seed-{number}") for number in range(1, 5)]
    for seed_number, seed in enumerate(seeds, start=1):
        vector = (float(seed_number), 0.0)
        repository.vectors[seed.memory_id] = vector
        repository.neighbors[vector] = [
            _candidate(seed_number * 100 + offset, request_id=f"neighbor-{seed_number}-{offset}")
            for offset in range(MAX_NEIGHBORS_PER_SEED)
        ]

    expanded = _expander(repository).expand(_USER, seeds)

    assert len(expanded) == MAX_NEIGHBORS
