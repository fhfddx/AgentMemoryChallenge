"""Bounded source conversation context for an opt-in retrieval experiment."""

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from masm.retrieval.reranker import RankedEvidence
from masm.storage.types import SourceMessageSnapshot


class SourceSnapshots(Protocol):
    def source_messages_for_positions(
        self, user_id: str, positions: Sequence[tuple[str, int]],
    ) -> dict[tuple[str, int], SourceMessageSnapshot]: ...


class SourceContextEnricher:
    def __init__(self, repository: SourceSnapshots, *, max_chars: int = 1200) -> None:
        if not 1 <= max_chars <= 4096:
            raise ValueError("max_chars must be in 1..4096")
        self._repository = repository
        self._max_chars = max_chars

    def enrich(self, user_id: str, ranked: Sequence[RankedEvidence]) -> list[RankedEvidence]:
        """Read adjacent positions only within the matched user's original Add request.

        A role labels a message; it does not establish the identity of any person
        mentioned in its text. No model, inferred relation, or cross-session join is used.
        """
        eligible = [item for item in ranked if (
            item.user_id == user_id and item.granularity == "message"
            and item.request_id and item.source_position is not None
            and item.source_position >= 0
        )]
        keys: list[tuple[str, int]] = []
        for item in eligible:
            assert item.source_position is not None
            keys.extend((item.request_id, position) for position in range(
                max(0, item.source_position - 1), item.source_position + 2,
            ))
        keys = list(dict.fromkeys(keys))
        snapshots: dict[tuple[str, int], SourceMessageSnapshot] = {}
        # Repository lookups cap at 256 keys. Batch explicitly so later candidates
        # do not silently lose context when 100 ranked candidates need 300 positions.
        for start in range(0, len(keys), 256):
            snapshots.update(self._repository.source_messages_for_positions(
                user_id, keys[start:start + 256]
            ))
        enriched = {}
        for item in eligible:
            assert item.source_position is not None
            position = item.source_position
            own = snapshots.get((item.request_id, position))
            if not _text_source(own):
                continue
            assert own is not None
            messages = [_turn(position, own)]
            neighbours = [p for p in (position - 1, position + 1) if p >= 0]
            omitted = [p for p in neighbours if (item.request_id, p) in snapshots]
            content = _serialize(position, messages, omitted)
            if len(content) > self._max_chars:
                continue
            for neighbour in neighbours:
                snapshot = snapshots.get((item.request_id, neighbour))
                if not _text_source(snapshot):
                    continue
                assert snapshot is not None
                attempt = sorted(
                    [*messages, _turn(neighbour, snapshot)], key=lambda m: m["position"],
                )
                remaining = [p for p in omitted if p != neighbour]
                encoded = _serialize(position, attempt, remaining)
                if len(encoded) <= self._max_chars:
                    messages, omitted, content = attempt, remaining, encoded
            enriched[item.memory_id] = replace(item, source_context=content)
        return [enriched.get(item.memory_id, item) if item.user_id == user_id else item
                for item in ranked]


def _text_source(snapshot: SourceMessageSnapshot | None) -> bool:
    return (snapshot is not None and snapshot.role in {"user", "assistant"}
            and isinstance(snapshot.content, str) and bool(snapshot.content.strip()))


def _turn(position: int, snapshot: SourceMessageSnapshot) -> dict:
    return {"position": position, "role": snapshot.role,
            "text": snapshot.content, "timestamp": snapshot.timestamp}


def _serialize(position: int, messages: list[dict], omitted: list[int]) -> str:
    # JSON keeps quoted instructions and forged role headers inside their source text.
    return json.dumps(
        {"matched_position": position, "messages": messages, "omitted_positions": omitted},
        ensure_ascii=False, separators=(",", ":"),
    )
