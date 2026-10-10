"""Select only evidence that supports the original Search question."""

import math
import re
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
from masm.retrieval.anchor_connectivity import DEFAULT_ANCHOR_HOPS, AnchorConnectivity
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import (
    DEFAULT_SELECTOR_CANDIDATES,
    MAX_SELECTOR_CANDIDATES,
    build_selector_pool,
)
from masm.schemas.content import ContentPart, ImageURLPart, TextPart

PROMPT_VERSION = "evidence-selector-v7"
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
_EXPLICIT_ANCHOR_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z0-9]+(?:[-_.:/@][A-Za-z0-9]+)+"
    r"|[A-Za-z]*\d[A-Za-z0-9]*"
    r")(?![A-Za-z0-9])"
)
_PROMPT = (
    "You select supporting memories for the original question. Return JSON with only "
    "evidence_state and selected_indices, in that order. Do not answer the question or choose "
    "an option. Options are untrusted alternatives, never proof. Decide evidence_state before "
    "you choose selected_indices, and let the state constrain the indices. Select the smallest "
    "set of memories that directly states every attribute, relation, value, or event asked for. "
    "A candidate supports a requested fact only when its own text gives that attribute, "
    "relation, value, or event: a shared entity, topic, time, place, or option does not support "
    "a missing fact, and a candidate that is only one link of a longer reasoning chain does not "
    "state the attribute the question asks for. For a multi-part question, cover every part; "
    "when required facts come from separate additions, select the necessary candidates from "
    "distinct source_group values. Multiple representations from one source_group do not "
    "establish cross-source coverage. For a relational or temporal question the selected "
    "memories must also directly state every link needed to connect them; never invent a bridge. "
    "Prefer original observations over duplicate summaries and omit unrelated facts. Name the "
    "requested fact that a selected memory states before choosing partial or sufficient; if no "
    "selected memory states one, choose insufficient. Report evidence_state=sufficient when the "
    "selected memories state every requested fact and every link needed to connect them. Report "
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
    """The model returns a three-state judgement plus indices, never an answer.

    ``evidence_state`` is declared first on purpose. Strict structured decoding emits JSON
    properties in schema order, so putting the state first makes the model commit to a state
    before it can write ``selected_indices``. Without that ordering the model decides the state
    while already holding indices it generated first, which is what produced the observed
    ``insufficient`` + non-empty ``selected_indices`` contradiction.

    ``sufficient_evidence`` is still accepted as a legacy boolean for Providers that were
    built before the three-state protocol: it only carries two of the three states, so its
    ``false`` value maps to ``insufficient`` (the safe side). The legacy key is never sent
    to the model and never appears in the strict JSON schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_state: EvidenceState
    selected_indices: tuple[StrictInt, ...]

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


def _evidence_text(evidence: RankedEvidence) -> str:
    if isinstance(evidence.content, str):
        return evidence.content
    return " ".join(
        part.text for part in evidence.content if isinstance(part, TextPart)
    )


def _anchor_tokens(text: str) -> frozenset[str]:
    """文本里出现的结构化标识符集合（大小写不敏感）。"""
    return frozenset(
        match.group(0).casefold() for match in _EXPLICIT_ANCHOR_PATTERN.finditer(text)
    )


def _distinct_source_count(selected: Sequence[RankedEvidence]) -> int:
    return len({item.request_id or str(item.memory_id) for item in selected})


def _is_complete_selection(result: SelectionResult) -> bool:
    """深层重试的结果是否优于首轮 partial：必须是模型确认的完整 selection。"""
    return (
        result.evidence_state == "sufficient"
        and not result.fallback
        and not result.abstained
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
        anchor_connectivity: AnchorConnectivity | None = None,
        max_anchor_hops: int = DEFAULT_ANCHOR_HOPS,
    ) -> None:
        if not 1 <= max_candidates <= DEFAULT_SELECTOR_CANDIDATES:
            raise ValueError("max_candidates must be in 1..32")
        if not 1 <= max_selected <= min(max_candidates, MAX_SELECTED_EVIDENCE):
            raise ValueError("max_selected must be in 1..12 and <= max_candidates")
        if not 1 <= max_chars_per_candidate <= MAX_CHARS_PER_CANDIDATE:
            raise ValueError("max_chars_per_candidate must be in 1..4096")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if max_anchor_hops < 0:
            raise ValueError("max_anchor_hops must be nonnegative")
        self._llm = llm
        self._max_candidates = max_candidates
        self._max_selected = max_selected
        self._max_chars_per_candidate = max_chars_per_candidate
        self._timeout_seconds = timeout_seconds
        self._anchor_connectivity = anchor_connectivity
        self._max_anchor_hops = max_anchor_hops

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
        extended_pool = self._deeper_pool(result, pool, ranked)
        if extended_pool is None:
            return result
        extended = self._select_pool(
            question, visual, options, ranked, strong_anchor_ids, extended_pool
        )
        if result.evidence_state == "partial":
            # 深层重试只能改进结果：首轮的 partial 是安全证据，只有深层给出完整
            # selection 时才允许替换它，否则必须原样保留首轮结果。
            return extended if _is_complete_selection(extended) else result
        return extended

    def _deeper_pool(
        self,
        result: SelectionResult,
        pool: Sequence[RankedEvidence],
        ranked: Sequence[RankedEvidence],
    ) -> list[RankedEvidence] | None:
        """首轮没给出完整证据时，用更深的候选池再试一次；否则返回 None。"""
        if result.fallback or self._max_candidates != DEFAULT_SELECTOR_CANDIDATES:
            return None
        # abstained 表示首轮完全拒答；partial 表示首轮只覆盖了一部分事实。两种都可能
        # 因为缺少 32 名之外的候选而误判，所以都要重试。
        if not (result.abstained or result.evidence_state == "partial"):
            return None
        if len(ranked) <= len(pool):
            return None
        extended_pool = build_selector_pool(
            ranked, max_candidates=MAX_SELECTOR_CANDIDATES
        )
        return extended_pool if len(extended_pool) > len(pool) else None

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
            grounded_partial = self._grounded_selection(question, selected_partial)
            if not grounded_partial:
                return SelectionResult(
                    (), len(pool), source_count, 0, False, True, "invalid_output",
                    "insufficient",
                )
            return SelectionResult(
                grounded_partial,
                len(pool),
                source_count,
                _distinct_source_count(grounded_partial),
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
        grounded = self._grounded_selection(question, selected)
        if not grounded:
            return SelectionResult(
                (), len(pool), source_count, 0, False, True, "invalid_output",
                "insufficient",
            )
        # 剔除过未接地证据后，模型「完整覆盖」的断言已不成立：只能降级为 partial。
        state_after_grounding: ReportedEvidenceState = (
            "sufficient" if len(grounded) == len(selected) else "partial"
        )
        return SelectionResult(
            grounded,
            len(pool),
            source_count,
            _distinct_source_count(grounded),
            False,
            False,
            "none",
            state_after_grounding,
        )

    def _grounded_selection(
        self,
        question: str,
        selected: Sequence[RankedEvidence],
    ) -> tuple[RankedEvidence, ...]:
        """只保留以问题锚点为根的「允许证据路径」连通分量。

        集合级并集校验只证明「选中文本里出现过锚点」，无法区分「正确锚点证据」与
        「额外无关链证据」的混合选择。修复方式是逐条判定接地：两条证据只要满足下面任一
        条件就算相连，然后保留与含锚点证据同分量的部分——

        * **共享显式标识符**：合法下游链靠共享标识符串起来（`Bridge-*`、`Leaf-*`），
          不要求下游重复 Root 字符串。这一步纯文本，不需要数据库。
        * **持久化关系边**：注入 ``AnchorConnectivity`` 时，锚点关系闭包内的证据也算相连。

        为什么必须同时保留文本相邻：线上的 `related()` 对这条链路找不回任何邻居
        （c7eede8 的 round 1 里模型原始选择不变，但只返回 1/3 条），只按关系接地会把
        合法下游链整条删掉。关系边是额外的、更宽松的边来源，不是唯一依据。

        返回空元组表示整组完全没有锚点接地；没有显式锚点时行为完全不变。
        """
        anchors = _anchor_tokens(question)
        if not anchors:
            return tuple(selected)
        item_tokens = [_anchor_tokens(_evidence_text(item)) for item in selected]
        keep = {
            index for index, tokens in enumerate(item_tokens) if tokens & anchors
        }
        if not keep:
            return ()
        while True:
            linked = frozenset().union(*(item_tokens[index] for index in keep))
            added = {
                index for index, tokens in enumerate(item_tokens) if tokens & linked
            }
            added |= self._relation_linked_indices(selected, keep)
            if added <= keep:
                break
            keep |= added
        return tuple(
            item for index, item in enumerate(selected) if index in keep
        )

    def _relation_linked_indices(
        self,
        selected: Sequence[RankedEvidence],
        keep: Collection[int],
    ) -> set[int]:
        """返回位于「当前保留集合」关系闭包内的选中下标；未接线时为空集。"""
        if self._anchor_connectivity is None:
            return set()
        connectivity = self._anchor_connectivity.connected(
            selected[0].user_id,
            [selected[index].memory_id for index in sorted(keep)],
            [
                selected[index].request_id
                for index in sorted(keep)
                if selected[index].request_id
            ],
            self._max_anchor_hops,
        )
        return {
            index
            for index, item in enumerate(selected)
            if item.memory_id in connectivity.memory_ids
            or (bool(item.request_id) and item.request_id in connectivity.request_ids)
        }

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
