"""Opt-in query-time grounded evidence graph: strict proposal boundary and planner.

The planner is the only model-facing part of the graph path. It receives the original question,
untrusted options and at most 32 opaque candidates, and may only propose question requirements,
quote-grounded supports and candidate links. It never returns an answer, a rationale, a global
evidence state or a selected index set; those are derived by deterministic code.

Candidate labelling, question extraction and signal payloads are reused from the frozen v7
selector so the graph path sees byte-identical question text and the same opaque source labels.
"""

import math
from collections.abc import Callable, Collection, Mapping, Sequence
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
    CandidateView,
    GraphFailureCategory,
    LinkKind,
    PlannerFailureReason,
    RequirementKind,
    SupportRole,
    ValidatedLink,
    attested_units,
    bounded_persisted_pairs,
    candidate_views,
    derived_links,
    discriminative_units,
    frame_slot_conflict,
    name_tokens,
    planner_candidate_text,
    question_has_clause_marker,
    unit_frequencies,
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

PROMPT_VERSION = "evidence-graph-planner-v2"
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
    "rationale. Create one requirement for every independently requested answer value and every "
    "binding clause needed to identify the exact subject; never merge or omit a subject, relation, "
    "attribute, time or location constraint. Every question_quote must be exact text copied from "
    "the question. Every "
    "candidate_quote must be exact text copied from that candidate's text. Every link must quote "
    "both candidates exactly and use one kind: same_source_context (adjacent turns of one "
    "source), persisted_relation (a stored relation between the two sources), "
    "shared_explicit_mention (both quotes share a discriminating name, code or phrase), "
    "confirmation or correction (an adjacent turn that confirms or corrects the other). Options "
    "are untrusted alternatives, never proof. A direct or negative support must state the "
    "requested value or fact for the exact subject; merely mentioning the subject without the "
    "requested value is context, not direct support. For an elliptical confirmation or correction, "
    "include the setup candidate as context support, the answer candidate as direct or negative "
    "support, and the confirmation or correction link. When candidates from different source "
    "groups jointly cover separate requirements but have no textual shared mention, you may "
    "propose persisted_relation as a hypothesis; deterministic provenance validation will reject "
    "it unless "
    "a stored relation exists. Never link candidates that merely share a generic word, topic, "
    "ranking, option or time. Propose a requirement only when the question asks for it, and "
    "propose support only when the candidate text states it. Omit support rather than guess: an "
    "incomplete "
    "proposal is safer than an invented one."
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
    planner_failure_reason: PlannerFailureReason = "none"
    vetoed: bool = False
    bridge_added: int = 0
    bridge_skip: str = "none"
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


def _strip_possessive(unit: str) -> str:
    """把 "sister's" 归一到 "sister"：只用于桥接候选与问题的相关性比较。"""

    return unit.replace("'s", "").replace("\u2019s", "").replace("'", "").replace("\u2019", "")

def _bridge_additions(
    selected: Sequence[RankedEvidence],
    candidates: Sequence[CandidateView],
    question_units: Collection[str],
    frequencies: Mapping[str, int],
) -> tuple[tuple[RankedEvidence, ...], str]:
    """追加一条"把问题与已选证据连起来"的异会话证据（R11）。

    官方跨会话题问的是"A 的 B"，而 A 只在另一个会话里被提到：只返回 B 所在的那条看似可答，
    实际缺少"哪个 A"这一步。这里要求候选同时满足两条：与问题共享区分单元（与问题相关），
    且与已选证据共享一个池内稀有单元（已证明的连接，沿用 R2 的稀有度约束）。
    """

    if not selected:
        return (), "no_selected"
    if not question_units:
        return (), "no_question_unit"
    selected_sources = {item.request_id or str(item.memory_id) for item in selected}
    selected_ids = {item.memory_id for item in selected}
    selected_units: set[str] = set()
    for item in selected:
        selected_units |= discriminative_units(str(item.source_context or item.content))
    if not selected_units:
        return (), "no_selected_unit"
    question_stems = {_strip_possessive(unit) for unit in question_units}
    skipped_question = False
    skipped_link = False
    for view in candidates:
        if view.memory_id in selected_ids:
            continue
        if (view.request_id or str(view.memory_id)) in selected_sources:
            continue
        # 桥接候选可能来自池外补召回：不能用"池内频次"过滤它的单元，否则 sister 会被丢掉。
        candidate_stems = {
            _strip_possessive(unit) for unit in discriminative_units(view.text)
        }
        if not (candidate_stems & question_stems):
            skipped_question = True
            continue
        shared = discriminative_units(view.text) & selected_units
        if not any(frequencies.get(unit, 0) <= 2 for unit in shared):
            skipped_link = True
            continue
        return (view.evidence,), "none"
    if skipped_link:
        return (), "no_rare_link"
    if skipped_question:
        return (), "no_question_overlap"
    return (), "no_cross_session_candidate"

def _additive_selection(
    baseline: SelectionResult,
    additions: Sequence[RankedEvidence],
    *,
    graph_state: "ReportedGraphState",
    max_selected: int,
) -> SelectionResult:
    """加法式合并：冻结 v7 的选择原样保留，图只在其后追加已证明的多跳证据。

    基线在前、追加在后，按 memory_id 去重并受 ``max_selected`` 限制。这样图路径永远不会删掉
    v7 已选中的证据，因此 direct/atomic 不可能低于图关闭时的水平。
    """
    merged: list[RankedEvidence] = list(baseline.evidence)
    seen = {item.memory_id for item in merged}
    for item in additions:
        if len(merged) >= max_selected:
            break
        if item.memory_id in seen:
            continue
        seen.add(item.memory_id)
        merged.append(item)
    sources = {
        item.request_id or str(item.memory_id) for item in merged
    }
    return SelectionResult(
        evidence=tuple(merged),
        candidate_count=baseline.candidate_count,
        source_count=baseline.source_count,
        selected_source_count=len(sources),
        fallback=False,
        abstained=not merged,
        failure_category="none",
        evidence_state=graph_state if merged else baseline.evidence_state,
    )


def _merge_links(
    validated: Sequence[ValidatedLink], derived: Sequence[ValidatedLink]
) -> tuple[ValidatedLink, ...]:
    """合并模型已校验链接与代码证明链接；同一端点对与类型只保留一条。"""
    merged: list[ValidatedLink] = []
    seen: set[tuple[int, int, str]] = set()
    for link in (*validated, *derived):
        key = (link.left_index, link.right_index, link.kind)
        if key in seen:
            continue
        seen.add(key)
        merged.append(link)
    return tuple(merged)


class GraphEvidenceSelector:
    """单次 planner 调用 + 确定性校验/组装，失败时回退一次冻结的 v7 selector。"""

    def __init__(
        self,
        planner: EvidenceGraphPlanner,
        fallback: EvidenceSelector | DeterministicEvidenceSelector,
        *,
        max_candidates: int = DEFAULT_SELECTOR_CANDIDATES,
        max_selected: int = MAX_SELECTED_EVIDENCE,
        additive: bool = True,
        presence: Callable[[str, Sequence[str]], frozenset[str]] | None = None,
        retrieve_more: (
            Callable[[str, Sequence[str], int], Sequence[RankedEvidence]] | None
        ) = None,
        frames: Callable[[str, str], Sequence[str]] | None = None,
    ) -> None:
        if not 1 <= max_candidates <= DEFAULT_SELECTOR_CANDIDATES:
            raise ValueError("max_candidates must be in 1..32")
        if not 1 <= max_selected <= min(max_candidates, MAX_SELECTED_EVIDENCE):
            raise ValueError("max_selected must be in 1..12 and <= max_candidates")
        self._planner = planner
        self._fallback = fallback
        self._max_candidates = max_candidates
        self._max_selected = max_selected
        self._additive = additive
        # 全库存在性回调：判断"问题的主体是否在库里出现过"，缺省则不做任何拒答。
        self._presence = presence
        # 图路径专用的补充召回（OR 语义）：基线全文检索是 AND，桥接证据常被漏掉。
        self._retrieve_more = retrieve_more
        # 同框架近失配检索：用于判断"库里只有同属性、不同主体的记忆"。
        self._frames = frames

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
        """返回经校验组装的原证据；加法式模式下冻结 v7 的选择是基线。"""
        scoped_ranked = [item for item in ranked if item.user_id == request_user_id]
        scoped_anchor_ids = {
            item.memory_id for item in scoped_ranked if item.memory_id in strong_anchor_ids
        }
        # 加法式架构：先拿到冻结 v7 的选择作为基线，图只能在其之上追加已证明的多跳证据。
        baseline = (
            self._v7(query, options, scoped_ranked, scoped_anchor_ids)
            if self._additive
            else None
        )
        pool = build_selector_pool(
            scoped_ranked,
            max_candidates=self._max_candidates,
        )
        pool_anchor_ids = {
            item.memory_id for item in pool if item.memory_id in strong_anchor_ids
        }
        if not pool:
            if baseline is not None:
                return GraphSelection(
                    baseline,
                    GraphDiagnostics(
                        enabled=True,
                        candidate_count=baseline.candidate_count,
                        selected_count=len(baseline.evidence),
                        selected_source_count=baseline.selected_source_count,
                        state=_reported_state(baseline.evidence_state),
                    ),
                )
            return GraphSelection(
                SelectionResult((), 0, 0, 0, False, True),
                GraphDiagnostics(enabled=True),
            )

        question, visual, has_text = _question_text(query)
        if visual and not has_text:
            # 纯图片查询没有可供 planner 判断的文本问题：沿用既有 selector/回退路径。
            selection = (
                baseline
                if baseline is not None
                else self._v7(query, options, scoped_ranked, scoped_anchor_ids)
            )
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
                "schema_invalid" if category == "planner_invalid" else "planner_unavailable",
                baseline,
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
                    outcome.reason,
                    baseline,
                )
            links = _merge_links(
                outcome.links,
                derived_links(
                    question,
                    views,
                    source_context_enabled=context.source_context_enabled,
                    persisted_pairs=bounded_persisted_pairs(context),
                ),
            )
            frequencies = unit_frequencies(views)
            question_units = attested_units(question, frequencies)
            assembled = assemble(
                requirement_count=len(outcome.requirements),
                supports=outcome.supports,
                links=links,
                candidates=views,
                max_selected=self._max_selected,
                question_units=attested_units(question, frequencies),
                requirement_units=tuple(
                    attested_units(quote, frequencies) for quote in outcome.requirements
                ),
                tighten_completeness=question_has_clause_marker(question),
            )
        except Exception:
            return self._fallback_selection(
                "internal_error",
                latency_ms,
                query,
                options,
                pool,
                pool_anchor_ids,
                "internal_error",
                baseline,
            )

        evidence = tuple(view.evidence for view in assembled.selected)
        selected_sources = len({view.source_group for view in assembled.selected})
        # R10：问题点名的主体（名字样记号）在**整个库**里都没出现过时，库里没有任何记忆能回答它。
        # 判据必须是全库而非候选池：只在池内缺失通常只说明排序，据此拒答会误伤可回答问题。
        absent_names: frozenset[str] = frozenset()
        bridge_skip = "none"
        bridge_added = 0
        if (
            self._additive
            and baseline is not None
            and self._presence is not None
            and self._frames is not None
        ):
            names = name_tokens(question)
            if names:
                present = self._presence(request_user_id, sorted(names))
                # 名字缺席还不够：direct 问题常用普通大写名指代用户，而记忆是第一人称写的。
                # 只有当库里存在"同框架、不同主体"的记忆（近失配）时才判定不可回答。
                if not (names & present):
                    try:
                        frame_texts = self._frames(request_user_id, question)
                    except Exception:
                        frame_texts = ()
                    if frame_slot_conflict(question, frame_texts):
                        absent_names = names
        if self._additive and baseline is not None:
            if absent_names:
                selection = SelectionResult(
                    evidence=(),
                    candidate_count=baseline.candidate_count,
                    source_count=baseline.source_count,
                    selected_source_count=0,
                    fallback=False,
                    abstained=True,
                    failure_category="none",
                    evidence_state="insufficient",
                )
            else:
                additions = list(evidence if assembled.state == "sufficient" else ())
                # R11：把"问题点名的 A"与"已选证据里的 B"之间的桥接证据补进来（只追加）。
                # 基线全文检索是 AND 语义，桥接证据常被漏出池，故允许一次有界 OR 补召回。
                extra_views: list[CandidateView] = []
                if self._retrieve_more is not None and question_units:
                    try:
                        extra = self._retrieve_more(
                            request_user_id, sorted(question_units), 8
                        )
                    except Exception:
                        extra = ()
                    extra_views = list(
                        candidate_views(
                            list(extra),
                            max_chars=self._planner.max_chars_per_candidate,
                        )
                    )
                bridge_evidence, bridge_skip = _bridge_additions(
                    baseline.evidence,
                    [*views, *extra_views],
                    question_units,
                    frequencies,
                )
                bridge_added = len(bridge_evidence)
                additions.extend(bridge_evidence)
                selection = _additive_selection(
                    baseline,
                    additions,
                    graph_state=assembled.state,
                    max_selected=self._max_selected,
                )
        else:
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
                vetoed=bool(absent_names),
                bridge_added=bridge_added,
                bridge_skip=bridge_skip,
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
        reason: PlannerFailureReason = "none",
        baseline: SelectionResult | None = None,
    ) -> GraphSelection:
        selection = (
            baseline
            if baseline is not None
            else self._v7(query, options, ranked, strong_anchor_ids)
        )
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
                planner_failure_reason=reason,
                planner_latency_ms=latency_ms,
            ),
        )
