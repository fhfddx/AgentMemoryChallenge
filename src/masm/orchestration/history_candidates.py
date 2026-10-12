"""Build a bounded, same-user history pool for Add-time agents."""

from collections.abc import Iterable, Sequence

from masm.agents import MAX_HISTORY, resolve_max_history
from masm.retrieval.baseline import BaselineRetriever, ParsedQuery
from masm.schemas.agents import PerceptionResult
from masm.schemas.api import AddRequest
from masm.schemas.content import TextPart
from masm.storage.types import MemoryCandidate

SEMANTIC_SLOTS = 6
RECENT_SLOTS = 2
MAX_TEXT_QUERIES = 4


class HistoryCandidateBuilder:
    """Combine stable semantic recall with a small recent-context tail."""

    def __init__(
        self, retriever: BaselineRetriever, *, max_history: int = MAX_HISTORY
    ) -> None:
        self._retriever = retriever
        self._max_history = resolve_max_history(max_history)

    def build(
        self, request: AddRequest, perception: PerceptionResult
    ) -> list[MemoryCandidate]:
        """Return at most ``max_history`` unique same-user context memories."""
        if self._max_history == 0:
            return []
        repository = self._retriever.repository
        if not repository.has_context_memory(request.user_id):
            return []

        entity_names = _stable_unique(entity.name for entity in perception.entities)
        keywords = _stable_unique(perception.keywords)
        metadata_entities = _stable_unique((*entity_names, *keywords))
        text_queries = _stable_unique(
            (
                _request_text(request),
                " ".join(entity_names),
                " ".join(keywords),
                " ".join(metadata_entities),
            )
        )[:MAX_TEXT_QUERIES]

        semantic = self._retriever.retrieve(
            request.user_id,
            ParsedQuery(
                text_queries=text_queries,
                entities=metadata_entities,
                intent="fact",
            ),
            self._max_history,
            granularity="context",
        )
        recent = repository.metadata_candidates(
            request.user_id,
            limit=self._max_history,
            granularity="context",
        )
        return _merge_candidates(
            semantic,
            recent,
            user_id=request.user_id,
            limit=self._max_history,
        )


def _request_text(request: AddRequest) -> str:
    parts: list[str] = []
    for message in request.messages:
        if isinstance(message.content, str):
            parts.append(message.content)
        else:
            parts.extend(
                part.text for part in message.content if isinstance(part, TextPart)
            )
    return _normalise_whitespace(" ".join(parts))


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalise_whitespace(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def _normalise_whitespace(value: str) -> str:
    return " ".join(value.split())


def _merge_candidates(
    semantic: Sequence[MemoryCandidate],
    recent: Sequence[MemoryCandidate],
    *,
    user_id: str,
    limit: int,
) -> list[MemoryCandidate]:
    valid_semantic = _valid_unique(semantic, user_id=user_id)
    valid_recent = _valid_unique(recent, user_id=user_id)

    semantic_slots = min(SEMANTIC_SLOTS, limit)
    result = list(valid_semantic[:semantic_slots])
    seen = {item.memory_id for item in result}
    semantic_ids = {item.memory_id for item in valid_semantic}

    recent_slots = min(RECENT_SLOTS, max(0, limit - len(result)))
    recent_added = 0
    for candidate in valid_recent:
        if recent_added >= recent_slots:
            break
        if candidate.memory_id in semantic_ids:
            continue
        result.append(candidate)
        seen.add(candidate.memory_id)
        recent_added += 1

    for candidate in valid_semantic[semantic_slots:]:
        if len(result) >= limit:
            break
        if candidate.memory_id not in seen:
            result.append(candidate)
            seen.add(candidate.memory_id)
    return result[:limit]


def _valid_unique(
    candidates: Sequence[MemoryCandidate], *, user_id: str
) -> list[MemoryCandidate]:
    result: list[MemoryCandidate] = []
    seen = set()
    for candidate in candidates:
        if candidate.user_id != user_id or candidate.granularity != "context":
            continue
        if candidate.memory_id in seen:
            continue
        seen.add(candidate.memory_id)
        result.append(candidate)
    return result
