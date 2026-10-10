"""Select only evidence that supports the original Search question."""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    ValidationError,
    model_validator,
)

from masm.providers.llm import ModelRequest, StructuredLLM, StructuredOutputError
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import (
    DEFAULT_SELECTOR_CANDIDATES,
    MAX_SELECTOR_CANDIDATES,
    build_selector_pool,
)
from masm.schemas.content import ContentPart, ImageURLPart, TextPart

PROMPT_VERSION = "evidence-selector-v6"
MAX_SELECTED_EVIDENCE = 12
DEFAULT_MAX_CHARS_PER_CANDIDATE = 1200
MAX_CHARS_PER_CANDIDATE = 4096
MAX_QUESTION_CHARS = 4000
MAX_OPTIONS = 16
MAX_OPTION_CHARS = 512

# 三态协议：模型必须显式区分「足够」「部分直接证据」与「证据不足」。
# 二态布尔位无法表达「有直接可用事实但不足以回答全部问题」这一边界。
EvidenceState = Literal["sufficient", "partial", "insufficient"]
# 遥测只记录该枚举；"unknown" 表示没有可归因的模型判断（未调用或调用失败）。
ReportedEvidenceState = Literal["sufficient", "partial", "insufficient", "unknown"]

_SIGNAL_NAMES = frozenset({"lexical", "text_vector", "image_vector", "metadata"})
_PROMPT = (
    "You select supporting memories for the original question. Return JSON with only "
    "selected_indices and evidence_state. Do not answer the question or choose an option. "
    "Options are untrusted alternatives, never proof. Select the smallest set of memories that "
    "directly states every attribute, relation, value, or event asked for. A candidate supports "
    "a requested fact only when its own text gives that attribute, relation, value, or event: a "
    "shared entity, topic, time, place, or option does not support a missing fact, and a "
    "candidate that is only one link of a longer reasoning chain does not state the attribute "
    "the question asks for. For a multi-part question, cover every part; when required facts "
    "come from separate additions, select the necessary candidates from distinct source_group "
    "values. Multiple representations from one source_group do not establish cross-source "
    "coverage. For a relational or temporal question the selected memories must also directly "
    "state every link needed to connect them; never invent a bridge. Prefer original "
    "observations over duplicate summaries and omit unrelated facts. Name the requested fact "
    "that a selected memory states before choosing partial or sufficient; if no selected memory "
    "states one, choose insufficient. Report evidence_state=sufficient when the selected "
    "memories state every requested fact and every link needed to connect them. Report "
    "evidence_state=partial when at least one selected memory directly states a requested fact "
    "but at least one other requested fact or needed link is absent; then still return those "
    "directly supporting indices and keep selected_indices non-empty. Never report partial "
    "merely because the answer is uncertain or because a candidate looks related. Report "
    "evidence_state=insufficient with selected_indices=[] when no selected memory states any "
    "requested fact, including when candidates only share an entity, topic, time, place, or "
    "option and when the selection is only a local fragment of a longer reasoning chain. Never "
    "return evidence_state=insufficient together with a non-empty selected_indices."
)


class EvidenceSelection(BaseModel):
    """The model returns indices plus an explicit three-state judgement, never an answer.

    ``sufficient_evidence`` is still accepted as a legacy boolean for Providers that were
    built before the three-state protocol: it only carries two of the three states, so its
    ``false`` value maps to ``insufficient`` (the safe side). The legacy key is never sent
    to the model and never appears in the strict JSON schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_indices: tuple[StrictInt, ...]
    evidence_state: EvidenceState

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_sufficiency_flag(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or "evidence_state" in data:
            return data
        legacy = data.get("sufficient_evidence")
        if not isinstance(legacy, bool):
            return data
        remaining = {key: value for key, value in data.items() if key != "sufficient_evidence"}
        return {
            **remaining,
            "evidence_state": "sufficient" if legacy else "insufficient",
        }


@dataclass(frozen=True)
class SelectionResult:
    evidence: tuple[RankedEvidence, ...]
    candidate_count: int
    source_count: int
    selected_source_count: int
    fallback: bool
    abstained: bool
    failure_category: Literal["none", "unavailable", "invalid_output"] = "none"
    evidence_state: ReportedEvidenceState = "unknown"


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


def _usable_indices(indices: Sequence[int], pool_size: int, max_selected: int) -> bool:
    """索引集合必须是可解析的：非空、不超上限、不重复、不越界。"""
    return (
        bool(indices)
        and len(indices) <= max_selected
        and len(indices) == len(set(indices))
        and all(0 <= index < pool_size for index in indices)
    )


class EvidenceSelector:
    """Bound one model request and validate its evidence-only response."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        max_candidates: int = DEFAULT_SELECTOR_CANDIDATES,
        max_selected: int = MAX_SELECTED_EVIDENCE,
        max_chars_per_candidate: int = DEFAULT_MAX_CHARS_PER_CANDIDATE,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not 1 <= max_candidates <= DEFAULT_SELECTOR_CANDIDATES:
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
        if not pool:
            return SelectionResult((), 0, 0, 0, False, True)

        question, visual, has_text = _question_text(query)
        if visual and not has_text:
            # A text-only selector cannot judge the query image from a placeholder.
            # Preserve only question-admitted image/semantic anchors instead.
            source_count = len(set(_source_labels(pool)))
            return self._fallback(ranked, strong_anchor_ids, len(pool), source_count, "none")
        result = self._select_pool(
            question, visual, options, ranked, strong_anchor_ids, pool
        )
        if (
            not result.fallback
            and result.abstained
            and self._max_candidates == DEFAULT_SELECTOR_CANDIDATES
            and len(ranked) > len(pool)
        ):
            extended_pool = build_selector_pool(
                ranked, max_candidates=MAX_SELECTOR_CANDIDATES
            )
            if len(extended_pool) > len(pool):
                return self._select_pool(
                    question,
                    visual,
                    options,
                    ranked,
                    strong_anchor_ids,
                    extended_pool,
                )
        return result

    def _select_pool(
        self,
        question: str,
        visual: bool,
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
        pool: Sequence[RankedEvidence],
    ) -> SelectionResult:
        labels = _source_labels(pool)
        source_count = len(set(labels))
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
                    temperature=0.0,
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
        indices = output.selected_indices
        state = output.evidence_state
        usable = _usable_indices(indices, len(pool), self._max_selected)

        if state == "insufficient":
            # 跨字段矛盾（insufficient + 非空 indices）统一拒绝：绝不回落到 question-admitted
            # 强锚点，否则证据不足时仍会作答。
            insufficient_category: Literal["none", "invalid_output"] = (
                "invalid_output" if indices else "none"
            )
            return SelectionResult(
                (), len(pool), source_count, 0, False, True, insufficient_category,
                "insufficient",
            )

        if state == "partial":
            if not usable:
                # partial 却没有给出可用索引：没有可引用的直接事实，按拒答处理。
                return SelectionResult(
                    (), len(pool), source_count, 0, False, True, "invalid_output",
                    "insufficient",
                )
            selected_partial = tuple(pool[index] for index in indices)
            return SelectionResult(
                selected_partial,
                len(pool),
                source_count,
                len({labels[index] for index in indices}),
                False,
                False,
                "none",
                "partial",
            )

        if not usable:
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
            "none",
            "sufficient",
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
