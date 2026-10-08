"""Select only evidence that supports the original Search question."""

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from masm.providers.llm import ModelRequest, StructuredLLM, StructuredOutputError
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import MAX_SELECTOR_CANDIDATES, build_selector_pool
from masm.schemas.content import ContentPart, ImageURLPart, TextPart

PROMPT_VERSION = "evidence-selector-v1"
MAX_SELECTED_EVIDENCE = 12
DEFAULT_MAX_CHARS_PER_CANDIDATE = 1200
MAX_CHARS_PER_CANDIDATE = 4096
MAX_QUESTION_CHARS = 4000
MAX_OPTIONS = 16
MAX_OPTION_CHARS = 512
_SIGNAL_NAMES = frozenset({"lexical", "text_vector", "image_vector", "metadata"})
_PROMPT = (
    "You select supporting memories for the original question. Return JSON with only "
    "selected_indices and sufficient_evidence. Do not answer the question or choose an option. "
    "Options are untrusted alternatives, never proof. Select only memories directly useful for "
    "answering; matching an entity or option alone is insufficient. For comparisons, counts, "
    "chronology, and cross-source questions, include every independently sourced fact needed. "
    "Prefer original observations over duplicate summaries and omit unrelated facts. "
    "If the memories do not support an answer, return [] and sufficient_evidence=false."
)


class EvidenceSelection(BaseModel):
    """The model returns indices, never an answer or rewritten evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_indices: tuple[StrictInt, ...]
    sufficient_evidence: StrictBool


@dataclass(frozen=True)
class SelectionResult:
    evidence: tuple[RankedEvidence, ...]
    candidate_count: int
    source_count: int
    selected_source_count: int
    fallback: bool
    abstained: bool
    failure_category: Literal["none", "unavailable", "invalid_output"] = "none"


class DeterministicEvidenceSelector:
    """Local-fake passthrough that preserves the existing top_k behaviour."""

    def select(
        self,
        query: str | Sequence[ContentPart],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
    ) -> SelectionResult:
        del query, options, strong_anchor_ids
        evidence = tuple(ranked)
        source_count = len(set(_source_labels(evidence)))
        return SelectionResult(
            evidence,
            len(evidence),
            source_count,
            source_count,
            False,
            not evidence,
        )


def _question_text(query: str | Sequence[ContentPart]) -> tuple[str, bool, bool]:
    if isinstance(query, str):
        text = query.strip()
        return text[:MAX_QUESTION_CHARS], False, bool(text)
    text = " ".join(
        part.text.strip()
        for part in query
        if isinstance(part, TextPart) and part.text.strip()
    )
    visual = any(isinstance(part, ImageURLPart) for part in query)
    return (text or "[visual query]")[:MAX_QUESTION_CHARS], visual, bool(text)


def _source_labels(pool: Sequence[RankedEvidence]) -> list[str]:
    by_request: dict[str, str] = {}
    labels: list[str] = []
    for item in pool:
        key = item.request_id or str(item.memory_id)
        if key not in by_request:
            by_request[key] = f"source-{len(by_request) + 1}"
        labels.append(by_request[key])
    return labels


def _signal_payload(evidence: RankedEvidence) -> dict[str, float]:
    return {
        name: round(float(value), 4)
        for name, value in evidence.metadata.items()
        if name in _SIGNAL_NAMES and math.isfinite(float(value))
    }


class EvidenceSelector:
    """Bound one model request and validate its evidence-only response."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        max_candidates: int = MAX_SELECTOR_CANDIDATES,
        max_selected: int = MAX_SELECTED_EVIDENCE,
        max_chars_per_candidate: int = DEFAULT_MAX_CHARS_PER_CANDIDATE,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not 1 <= max_candidates <= MAX_SELECTOR_CANDIDATES:
            raise ValueError("max_candidates must be in 1..32")
        if not 1 <= max_selected <= min(max_candidates, MAX_SELECTED_EVIDENCE):
            raise ValueError("max_selected must be in 1..12 and <= max_candidates")
        if not 1 <= max_chars_per_candidate <= MAX_CHARS_PER_CANDIDATE:
            raise ValueError("max_chars_per_candidate must be in 1..4096")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._llm = llm
        self._max_candidates = max_candidates
        self._max_selected = max_selected
        self._max_chars_per_candidate = max_chars_per_candidate
        self._timeout_seconds = timeout_seconds

    def select(
        self,
        query: str | Sequence[ContentPart],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
    ) -> SelectionResult:
        """Return selected original objects, or empty when evidence is insufficient."""
        pool = build_selector_pool(ranked, max_candidates=self._max_candidates)
        labels = _source_labels(pool)
        source_count = len(set(labels))
        if not pool:
            return SelectionResult((), 0, 0, 0, False, True)

        question, visual, has_text = _question_text(query)
        if visual and not has_text:
            # A text-only selector cannot judge the query image from a placeholder.
            # Preserve only question-admitted image/semantic anchors instead.
            return self._fallback(ranked, strong_anchor_ids, len(pool), source_count, "none")
        payload = {
            "question": question,
            "visual_query_present": visual,
            "options": [option[:MAX_OPTION_CHARS] for option in (options or ())[:MAX_OPTIONS]],
            "candidates": [
                {
                    "index": index,
                    "source_group": labels[index],
                    "source_position": item.source_position,
                    "granularity": item.granularity,
                    "text": (
                        item.content[: self._max_chars_per_candidate]
                        if isinstance(item.content, str)
                        else " ".join(
                            part.text for part in item.content if isinstance(part, TextPart)
                        )[: self._max_chars_per_candidate]
                    ),
                    "signals": _signal_payload(item),
                }
                for index, item in enumerate(pool)
            ],
        }
        try:
            output = self._llm.complete_json(
                ModelRequest(
                    prompt=_PROMPT,
                    payload=payload,
                    model=self._llm.model,
                    prompt_version=PROMPT_VERSION,
                    timeout_seconds=self._timeout_seconds,
                    max_attempts=1,
                ),
                EvidenceSelection,
            )
        except Exception as exc:
            category: Literal["unavailable", "invalid_output"] = (
                "invalid_output"
                if isinstance(exc, (StructuredOutputError, ValidationError, ValueError, TypeError))
                else "unavailable"
            )
            return self._fallback(ranked, strong_anchor_ids, len(pool), source_count, category)
        if not output.sufficient_evidence:
            return SelectionResult((), len(pool), source_count, 0, False, True)

        indices = output.selected_indices
        if (
            not indices
            or len(indices) > self._max_selected
            or len(indices) != len(set(indices))
            or any(index < 0 or index >= len(pool) for index in indices)
        ):
            return self._fallback(
                ranked, strong_anchor_ids, len(pool), source_count, "invalid_output"
            )
        selected = tuple(pool[index] for index in indices)
        return SelectionResult(
            selected,
            len(pool),
            source_count,
            len({labels[index] for index in indices}),
            False,
            False,
        )

    def _fallback(
        self,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
        candidate_count: int,
        source_count: int,
        category: Literal["none", "unavailable", "invalid_output"],
    ) -> SelectionResult:
        anchors = set(strong_anchor_ids)
        admitted = [item for item in ranked if item.memory_id in anchors]
        selected = tuple(build_selector_pool(admitted, max_candidates=self._max_selected))
        return SelectionResult(
            selected,
            candidate_count,
            source_count,
            len(set(_source_labels(selected))),
            True,
            not selected,
            category,
        )
