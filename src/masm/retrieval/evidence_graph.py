"""Opt-in query-time grounded evidence graph: strict proposal boundary and planner.

The planner is the only model-facing part of the graph path. It receives the original question,
untrusted options and at most 32 opaque candidates, and may only propose question requirements,
quote-grounded supports and candidate links. It never returns an answer, a rationale, a global
evidence state or a selected index set; those are derived by deterministic code.

Candidate labelling, question extraction and signal payloads are reused from the frozen v7
selector so the graph path sees byte-identical question text and the same opaque source labels.
"""

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from masm.providers.llm import ModelRequest, StructuredLLM, StructuredOutputError
from masm.retrieval.evidence_graph_assembly import assemble
from masm.retrieval.evidence_graph_rules import (
    MAX_GRAPH_CONTEXT_PAIRS,
    MAX_GRAPH_LINKS,
    MAX_GRAPH_REQUIREMENTS,
    MAX_GRAPH_SUPPORTS,
    GraphFailureCategory,
    LinkKind,
    RequirementKind,
    SupportRole,
    candidate_views,
    planner_candidate_text,
    validate_proposal,
)
from masm.retrieval.evidence_selector import (
    DEFAULT_MAX_CHARS_PER_CANDIDATE,
    MAX_CHARS_PER_CANDIDATE,
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MAX_SELECTED_EVIDENCE,
    DeterministicEvidenceSelector,
    EvidenceSelector,
    SelectionResult,
    _question_text,
    _signal_payload,
    _source_labels,
)
from masm.retrieval.relation_expander import ExpansionEdge
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import DEFAULT_SELECTOR_CANDIDATES, build_selector_pool
from masm.schemas.content import ContentPart
from masm.storage.types import MemoryCandidate

PROMPT_VERSION = "evidence-graph-planner-v1"
# 遥测只记录该固定枚举；"unknown" 表示本次没有图判定（纯图片查询或 v7 回退）。
ReportedGraphState = Literal["sufficient", "partial", "insufficient", "unknown"]

__all__ = [
    "MAX_GRAPH_LINKS",
    "MAX_GRAPH_REQUIREMENTS",
    "MAX_GRAPH_SUPPORTS",
    "MAX_SELECTED_EVIDENCE",
    "PROMPT_VERSION",
    "EvidenceGraphPlanner",
    "EvidenceGraphProposal",
    "GraphContext",
    "GraphDiagnostics",
    "GraphEvidenceSelector",
    "GraphSelection",
    "GroundedSupport",
    "ProposedLink",
    "QuestionRequirement",
    "SelectionResult",
    "source_pairs_from_edges",
    "GraphFailureCategory",
    "LinkKind",
    "RequirementKind",
    "SupportRole",
]

_PROMPT = (
    "You propose grounded evidence requirements and links for the original question. Return JSON "
    "with only requirements, supports and links. Do not answer the question, do not choose an "
    "option, and never output an evidence state, selected indices, a confidence score or a "
    "rationale. Every question_quote must be exact text copied from the question. Every "
    "candidate_quote must be exact text copied from that candidate's text. Every link must quote "
    "both candidates exactly and use one kind: same_source_context (adjacent turns of one "
    "source), persisted_relation (a stored relation between the two sources), "
    "shared_explicit_mention (both quotes share a discriminating name, code or phrase), "
    "confirmation or correction (an adjacent turn that confirms or corrects the other). Options "
    "are untrusted alternatives, never proof. Never link candidates that merely share a generic "
    "word, topic, ranking, option or time. Propose a requirement only when the question asks for "
    "it, and propose support only when the candidate text states it. Omit support rather than "
    "guess: an incomplete proposal is safer than an invented one."
)


class QuestionRequirement(BaseModel):
    """A unit of the question the answer must satisfy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_quote: str
    kind: RequirementKind


class GroundedSupport(BaseModel):
    """A candidate quote offered as evidence for one requirement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_index: StrictInt
    candidate_index: StrictInt
    candidate_quote: str
    role: SupportRole


class ProposedLink(BaseModel):
    """A proposed connection between two candidates, quoted on both sides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left_candidate_index: StrictInt
    right_candidate_index: StrictInt
    left_quote: str
    right_quote: str
    kind: LinkKind


class EvidenceGraphProposal(BaseModel):
    """The complete planner output; structural caps are enforced by the validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirements: tuple[QuestionRequirement, ...]
    supports: tuple[GroundedSupport, ...]
    links: tuple[ProposedLink, ...]


@dataclass(frozen=True)
class GraphContext:
    """Request-scoped, user-filtered provable connections; never sent to the planner."""

    relation_source_pairs: tuple[tuple[str, str], ...] = ()
    source_context_enabled: bool = False


def source_pairs_from_edges(
    edges: Sequence[ExpansionEdge],
    candidates: Sequence[MemoryCandidate],
    user_id: str,
) -> tuple[tuple[str, str], ...]:
    """把持久化关系边转换为来源分组对；跨用户或未知端点一律丢弃。

    planner 只能看到不透明来源标签，只有确定性校验器能看到这些真实来源对。
    """
    by_id = {
        item.memory_id: item for item in candidates if item.user_id == user_id
    }
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        left = by_id.get(edge.predecessor_id)
        right = by_id.get(edge.candidate_id)
        if left is None or right is None:
            continue
        if not left.request_id or not right.request_id:
            continue
        pair = (
            (left.request_id, right.request_id)
            if left.request_id <= right.request_id
            else (right.request_id, left.request_id)
        )
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= MAX_GRAPH_CONTEXT_PAIRS:
            break
    return tuple(pairs)


class EvidenceGraphPlanner:
    """Bound one structured planner request; the provider call happens here and nowhere else."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        max_candidates: int = DEFAULT_SELECTOR_CANDIDATES,
        max_chars_per_candidate: int = DEFAULT_MAX_CHARS_PER_CANDIDATE,
        max_requirements: int = MAX_GRAPH_REQUIREMENTS,
        max_supports: int = MAX_GRAPH_SUPPORTS,
        max_links: int = MAX_GRAPH_LINKS,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not 1 <= max_candidates <= DEFAULT_SELECTOR_CANDIDATES:
            raise ValueError("max_candidates must be in 1..32")
        if not 1 <= max_chars_per_candidate <= MAX_CHARS_PER_CANDIDATE:
            raise ValueError("max_chars_per_candidate must be in 1..4096")
        if not 1 <= max_requirements <= MAX_GRAPH_REQUIREMENTS:
            raise ValueError("max_requirements must be in 1..4")
        if not 1 <= max_supports <= MAX_GRAPH_SUPPORTS:
            raise ValueError("max_supports must be in 1..24")
        if not 1 <= max_links <= MAX_GRAPH_LINKS:
            raise ValueError("max_links must be in 1..24")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._llm = llm
        self._max_candidates = max_candidates
        self._max_chars_per_candidate = max_chars_per_candidate
        self._max_requirements = max_requirements
        self._max_supports = max_supports
        self._max_links = max_links
        self._timeout_seconds = timeout_seconds

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    @property
    def max_candidates(self) -> int:
        return self._max_candidates

    @property
    def max_chars_per_candidate(self) -> int:
        return self._max_chars_per_candidate

    @property
    def max_requirements(self) -> int:
        return self._max_requirements

    @property
    def max_supports(self) -> int:
        return self._max_supports

    @property
    def max_links(self) -> int:
        return self._max_links

    def plan(
        self,
        question: str,
        visual: bool,
        options: Sequence[str] | None,
        pool: Sequence[RankedEvidence],
    ) -> EvidenceGraphProposal:
        """Issue the single planner request for this Search."""
        labels = _source_labels(pool)
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
                    "text": planner_candidate_text(item, self._max_chars_per_candidate),
                    "signals": _signal_payload(item),
                }
                for index, item in enumerate(pool)
            ],
        }
        return self._llm.complete_json(
            ModelRequest(
                prompt=_PROMPT,
                payload=payload,
                model=self._llm.model,
                prompt_version=PROMPT_VERSION,
                timeout_seconds=self._timeout_seconds,
                max_attempts=1,
                temperature=0.0,
            ),
            EvidenceGraphProposal,
        )


@dataclass(frozen=True)
class GraphDiagnostics:
    """内容安全的图路径遥测：只有固定枚举、计数与耗时。"""

    enabled: bool = False
    planner_calls: int = 0
    candidate_count: int = 0
    requirement_count: int = 0
    support_count: int = 0
    link_count: int = 0
    component_count: int = 0
    selected_count: int = 0
    selected_source_count: int = 0
    state: "ReportedGraphState" = "unknown"
    fallback: bool = False
    failure_category: GraphFailureCategory = "none"
    planner_latency_ms: float = 0.0


@dataclass(frozen=True)
class GraphSelection:
    """图路径结果：兼容既有 `SelectionResult` 契约 + 内容安全遥测。"""

    selection: SelectionResult
    graph: GraphDiagnostics


def _reported_state(state: str) -> "ReportedGraphState":
    if state == "sufficient":
        return "sufficient"
    if state == "partial":
        return "partial"
    if state == "insufficient":
        return "insufficient"
    return "unknown"


class GraphEvidenceSelector:
    """单次 planner 调用 + 确定性校验/组装，失败时回退一次冻结的 v7 selector。"""

    def __init__(
        self,
        planner: EvidenceGraphPlanner,
        fallback: EvidenceSelector | DeterministicEvidenceSelector,
        *,
        max_candidates: int = DEFAULT_SELECTOR_CANDIDATES,
        max_selected: int = MAX_SELECTED_EVIDENCE,
    ) -> None:
        if not 1 <= max_candidates <= DEFAULT_SELECTOR_CANDIDATES:
            raise ValueError("max_candidates must be in 1..32")
        if not 1 <= max_selected <= min(max_candidates, MAX_SELECTED_EVIDENCE):
            raise ValueError("max_selected must be in 1..12 and <= max_candidates")
        self._planner = planner
        self._fallback = fallback
        self._max_candidates = max_candidates
        self._max_selected = max_selected

    def select(
        self,
        query: str | Sequence[ContentPart],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
        graph_context: GraphContext | None = None,
        *,
        request_user_id: str,
    ) -> GraphSelection:
        """返回经校验组装的原证据；valid insufficient 直接拒答且不回退。"""
        scoped_ranked = [item for item in ranked if item.user_id == request_user_id]
        pool = build_selector_pool(
            scoped_ranked,
            max_candidates=self._max_candidates,
        )
        pool_anchor_ids = {
            item.memory_id for item in pool if item.memory_id in strong_anchor_ids
        }
        if not pool:
            return GraphSelection(
                SelectionResult((), 0, 0, 0, False, True),
                GraphDiagnostics(enabled=True),
            )

        question, visual, has_text = _question_text(query)
        if visual and not has_text:
            # 纯图片查询没有可供 planner 判断的文本问题：沿用既有 selector/回退路径。
            selection = self._v7(query, options, pool, pool_anchor_ids)
            return GraphSelection(
                selection,
                GraphDiagnostics(
                    enabled=True,
                    candidate_count=selection.candidate_count,
                    selected_count=len(selection.evidence),
                    selected_source_count=selection.selected_source_count,
                    state=_reported_state(selection.evidence_state),
                ),
            )

        context = graph_context if graph_context is not None else GraphContext()
        views = candidate_views(pool, max_chars=self._planner.max_chars_per_candidate)
        started = perf_counter()
        try:
            proposal = self._planner.plan(question, visual, options, pool)
        except Exception as exc:
            category: GraphFailureCategory = (
                "planner_invalid"
                if isinstance(
                    exc, (StructuredOutputError, ValidationError, ValueError, TypeError)
                )
                else "planner_unavailable"
            )
            return self._fallback_selection(
                category,
                (perf_counter() - started) * 1000.0,
                query,
                options,
                pool,
                pool_anchor_ids,
            )
        latency_ms = (perf_counter() - started) * 1000.0
        try:
            outcome = validate_proposal(
                proposal,
                question=question,
                candidates=views,
                context=context,
                expected_user_id=request_user_id,
                max_requirements=self._planner.max_requirements,
                max_supports=self._planner.max_supports,
                max_links=self._planner.max_links,
            )
            if not outcome.valid:
                return self._fallback_selection(
                    "planner_invalid",
                    latency_ms,
                    query,
                    options,
                    pool,
                    pool_anchor_ids,
                )
            assembled = assemble(
                requirement_count=len(outcome.requirements),
                supports=outcome.supports,
                links=outcome.links,
                candidates=views,
                max_selected=self._max_selected,
            )
        except Exception:
            return self._fallback_selection(
                "internal_error",
                latency_ms,
                query,
                options,
                pool,
                pool_anchor_ids,
            )

        evidence = tuple(view.evidence for view in assembled.selected)
        selected_sources = len({view.source_group for view in assembled.selected})
        selection = SelectionResult(
            evidence=evidence,
            candidate_count=len(pool),
            source_count=len({view.source_group for view in views}),
            selected_source_count=selected_sources,
            fallback=False,
            abstained=assembled.state == "insufficient" or not evidence,
            failure_category="none",
            evidence_state=assembled.state,
        )
        return GraphSelection(
            selection,
            GraphDiagnostics(
                enabled=True,
                planner_calls=1,
                candidate_count=len(pool),
                requirement_count=len(outcome.requirements),
                support_count=len(outcome.supports),
                link_count=len(outcome.links),
                component_count=assembled.component_count,
                selected_count=len(evidence),
                selected_source_count=selected_sources,
                state=assembled.state,
                fallback=False,
                failure_category="none",
                planner_latency_ms=latency_ms,
            ),
        )

    def _v7(
        self,
        query: str | Sequence[ContentPart],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
    ) -> SelectionResult:
        """调用冻结的 v7 selector；任何异常都保持其既有语义。"""
        return self._fallback.select(query, options, ranked, strong_anchor_ids)

    def _fallback_selection(
        self,
        category: GraphFailureCategory,
        latency_ms: float,
        query: str | Sequence[ContentPart],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: Collection[UUID],
    ) -> GraphSelection:
        selection = self._v7(query, options, ranked, strong_anchor_ids)
        return GraphSelection(
            selection,
            GraphDiagnostics(
                enabled=True,
                planner_calls=1,
                candidate_count=selection.candidate_count,
                selected_count=len(selection.evidence),
                selected_source_count=selection.selected_source_count,
                state="unknown",
                fallback=True,
                failure_category=category,
                planner_latency_ms=latency_ms,
            ),
        )
