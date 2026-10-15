"""Opt-in query-time grounded evidence graph: strict proposal boundary and planner.

The planner is the only model-facing part of the graph path. It receives the original question,
untrusted options and at most 32 opaque candidates, and may only propose question requirements,
quote-grounded supports and candidate links. It never returns an answer, a rationale, a global
evidence state or a selected index set; those are derived by deterministic code.

Candidate labelling, question extraction and signal payloads are reused from the frozen v7
selector so the graph path sees byte-identical question text and the same opaque source labels.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, StrictInt

from masm.providers.llm import ModelRequest, StructuredLLM
from masm.retrieval.evidence_graph_rules import (
    MAX_GRAPH_LINKS,
    MAX_GRAPH_REQUIREMENTS,
    MAX_GRAPH_SUPPORTS,
    GraphFailureCategory,
    LinkKind,
    RequirementKind,
    SupportRole,
    planner_candidate_text,
)
from masm.retrieval.evidence_selector import (
    DEFAULT_MAX_CHARS_PER_CANDIDATE,
    MAX_CHARS_PER_CANDIDATE,
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MAX_SELECTED_EVIDENCE,
    SelectionResult,
    _signal_payload,
    _source_labels,
)
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import DEFAULT_SELECTOR_CANDIDATES

PROMPT_VERSION = "evidence-graph-planner-v1"

__all__ = [
    "MAX_GRAPH_LINKS",
    "MAX_GRAPH_REQUIREMENTS",
    "MAX_GRAPH_SUPPORTS",
    "MAX_SELECTED_EVIDENCE",
    "PROMPT_VERSION",
    "EvidenceGraphPlanner",
    "EvidenceGraphProposal",
    "GraphContext",
    "GroundedSupport",
    "ProposedLink",
    "QuestionRequirement",
    "SelectionResult",
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
