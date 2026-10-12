"""Bounded additive expansion through stored text-embedding neighbors."""

import math
from collections.abc import Collection, Sequence
from dataclasses import replace
from uuid import UUID

from masm.retrieval.baseline import TEXT_VECTOR_CHANNEL
from masm.retrieval.relevance import DEFAULT_MIN_TEXT_SIMILARITY
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate

MAX_SEEDS = 4
MAX_NEIGHBORS_PER_SEED = 8
MAX_NEIGHBORS = 24


def _bounded(value: int | None, hard_cap: int, name: str) -> int:
    if value is None:
        return hard_cap
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return min(value, hard_cap)


class SemanticNeighborExpander:
    """Recall same-user semantic neighbors from requests other than the seeds'."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        model_name: str,
        model_version: str,
        max_seeds: int | None = None,
        max_neighbors_per_seed: int | None = None,
        max_neighbors: int | None = None,
        min_similarity: float = DEFAULT_MIN_TEXT_SIMILARITY,
    ) -> None:
        similarity = float(min_similarity)
        if not math.isfinite(similarity) or not -1.0 <= similarity <= 1.0:
            raise ValueError("min_similarity must be a finite cosine similarity in [-1, 1]")
        self._repo = repository
        self._model_name = model_name
        self._model_version = model_version
        self._max_seeds = _bounded(max_seeds, MAX_SEEDS, "max_seeds")
        self._neighbors_per_seed = _bounded(
            max_neighbors_per_seed,
            MAX_NEIGHBORS_PER_SEED,
            "max_neighbors_per_seed",
        )
        self._max_neighbors = _bounded(max_neighbors, MAX_NEIGHBORS, "max_neighbors")
        self._min_similarity = similarity

    def expand(
        self,
        user_id: str,
        seeds: Sequence[MemoryCandidate],
        *,
        known_ids: Collection[UUID] = (),
    ) -> list[MemoryCandidate]:
        """Return bounded cross-request neighbors without changing direct candidates."""
        if self._max_seeds < 1 or self._neighbors_per_seed < 1 or self._max_neighbors < 1:
            return []
        bounded_seeds = self._select_seeds(user_id, seeds)
        if not bounded_seeds:
            return []

        vectors = self._repo.text_embedding_vectors(
            user_id,
            [seed.memory_id for seed in bounded_seeds],
            model_name=self._model_name,
            model_version=self._model_version,
        )
        if not vectors:
            return []

        excluded_requests = tuple(seed.request_id for seed in bounded_seeds)
        blocked_ids = {*known_ids, *(seed.memory_id for seed in bounded_seeds)}
        by_id: dict[UUID, MemoryCandidate] = {}
        for seed in bounded_seeds:
            vector = vectors.get(seed.memory_id)
            if vector is None:
                continue
            for candidate in self._repo.vector_candidates(
                user_id,
                vector,
                modality="text",
                model_name=self._model_name,
                model_version=self._model_version,
                limit=self._neighbors_per_seed,
                exclude_request_ids=excluded_requests,
            ):
                if (
                    candidate.user_id != user_id
                    or not candidate.request_id
                    or candidate.request_id in excluded_requests
                    or candidate.memory_id in blocked_ids
                    or candidate.score < self._min_similarity
                ):
                    continue
                similarity = candidate.score
                signaled = replace(
                    candidate,
                    score=round(seed.score * max(0.0, min(1.0, similarity)), 6),
                    retrieval_signals={
                        **candidate.retrieval_signals,
                        TEXT_VECTOR_CHANNEL: similarity,
                    },
                )
                current = by_id.get(candidate.memory_id)
                if current is None or signaled.score > current.score:
                    by_id[candidate.memory_id] = signaled

        return list(by_id.values())[: self._max_neighbors]

    def _select_seeds(
        self, user_id: str, seeds: Sequence[MemoryCandidate]
    ) -> list[MemoryCandidate]:
        selected: list[MemoryCandidate] = []
        seen_requests: set[str] = set()
        for seed in seeds:
            if seed.user_id != user_id or not seed.request_id or seed.request_id in seen_requests:
                continue
            seen_requests.add(seed.request_id)
            selected.append(seed)
            if len(selected) >= self._max_seeds:
                break
        return selected
