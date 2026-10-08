"""Deterministic, bounded candidate pool with room for independent sources."""

from collections.abc import Sequence
from uuid import UUID

from masm.retrieval.reranker import RankedEvidence

MAX_SELECTOR_CANDIDATES = 32


def build_selector_pool(
    ranked: Sequence[RankedEvidence], *, max_candidates: int = MAX_SELECTOR_CANDIDATES
) -> list[RankedEvidence]:
    """Reserve half the budget for distinct sources, then fill by overall rank."""
    if max_candidates < 0:
        raise ValueError("max_candidates must be nonnegative")
    limit = min(max_candidates, MAX_SELECTOR_CANDIDATES)
    if limit == 0:
        return []

    selected_ids: set[UUID] = set()
    seen_sources: set[str] = set()
    reservation = limit // 2
    for item in ranked:
        if len(seen_sources) >= reservation:
            break
        if not item.request_id or item.request_id in seen_sources:
            continue
        if item.memory_id in selected_ids:
            continue
        seen_sources.add(item.request_id)
        selected_ids.add(item.memory_id)

    for item in ranked:
        if len(selected_ids) >= limit:
            break
        selected_ids.add(item.memory_id)

    result: list[RankedEvidence] = []
    emitted: set[UUID] = set()
    for item in ranked:
        if item.memory_id in selected_ids and item.memory_id not in emitted:
            result.append(item)
            emitted.add(item.memory_id)
    return result
