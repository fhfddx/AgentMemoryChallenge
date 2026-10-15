"""Deterministic grounding rules for the opt-in query-time evidence graph.

The planner only proposes; this module decides what is provable. Quotes are matched against
the exact text the planner saw, and a candidate pair may only be connected by adjacent
same-source context, a persisted relation proven by the expansion sidecar, or an explicit
discriminative mention that is absent from the question. Generic relation nouns, stopwords,
short numbers and punctuation never connect anything, so an unprovable bridge fails closed.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from masm.retrieval.evidence_selector import _source_labels
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import TextPart

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from masm.retrieval.evidence_graph import EvidenceGraphProposal

# 数量硬上限：与设计规格第 7.2 节一致，配置只能收紧不能放宽。
MAX_GRAPH_REQUIREMENTS = 4
MAX_GRAPH_SUPPORTS = 24
MAX_GRAPH_LINKS = 24
MAX_GRAPH_CONTEXT_PAIRS = 64
_MAX_CJK_UNIT = 8

RequirementKind = Literal[
    "subject", "attribute", "relation", "time", "location", "count", "comparison"
]
SupportRole = Literal["direct", "bridge", "context", "negative"]
LinkKind = Literal[
    "same_source_context",
    "persisted_relation",
    "shared_explicit_mention",
    "confirmation",
    "correction",
]
GraphFailureCategory = Literal[
    "none", "planner_unavailable", "planner_invalid", "internal_error"
]

# 只影响确定性连接判定的最小多语言词表；不引入任何 NLP 依赖。
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "did", "do", "does",
        "for", "from", "had", "has", "have", "he", "her", "him", "his", "i", "if", "in", "is",
        "it", "its", "me", "my", "of", "on", "or", "our", "she", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "to", "was", "we", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "would", "you", "your",
    }
)
_GENERIC_LATIN_UNITS = frozenset(
    {
        "activity", "appointment", "arrangement", "city", "class", "code", "course", "date",
        "event", "item", "location", "meeting", "note", "plan", "place", "project", "report",
        "schedule", "summary", "thing", "time", "trip", "venue", "workshop",
    }
)
_GENERIC_CJK_UNITS = frozenset(
    {
        "工作", "工作坊", "作坊", "项目", "会议", "城市", "课程", "活动", "旅行", "聚会",
        "计划", "安排", "报告", "时间", "地点", "事情",
    }
)
_CJK_FUNCTION_CHARS = frozenset(
    "的了是在我你他她它们这那个和与及或有没不也都就很会要把被从到对为以之其而但吗呢吧啊呀嘛着过又再还只让给用上下里中外前后时些"
)

_LATIN_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_DIGITS = re.compile(r"[0-9]+")


def normalize_text(text: str) -> str:
    """NFKC + casefold + 空白折叠；绝不删除否定词或改变语义。"""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def grounded_quote(quote: str, text: str) -> bool:
    """引用必须非空且是 planner 实际所见文本的归一化精确子串。"""
    normalized = normalize_text(quote)
    return bool(normalized) and normalized in normalize_text(text)


def _is_content_token(token: str) -> bool:
    """既非停用词/泛化名词，也非过短数字：可以作为区分单元的一部分。"""
    if token in _STOPWORDS or token in _GENERIC_LATIN_UNITS:
        return False
    if _DIGITS.fullmatch(token):
        return len(token) >= 4
    return True


def _accept_latin_token(token: str) -> bool:
    """单 token 只有达到 6 字符（或 4 位以上数字）才具备区分度；多词短语见 `_is_content_token`。"""
    if not _is_content_token(token):
        return False
    return _DIGITS.fullmatch(token) is not None or len(token) >= 6


def _generic_cjk_unit(unit: str) -> bool:
    if any(char in _CJK_FUNCTION_CHARS for char in unit):
        return True
    return any(unit in generic for generic in _GENERIC_CJK_UNITS)


def discriminative_units(text: str) -> frozenset[str]:
    """显式且具备区分度的候选单元：拉丁多词短语或 >=6 字符词、CJK 连续片段。"""
    normalized = normalize_text(text)
    units: set[str] = set()
    tokens = [
        (match.group(), match.start(), match.end())
        for match in _LATIN_TOKEN.finditer(normalized)
    ]
    for token, _, _ in tokens:
        if _accept_latin_token(token):
            units.add(token)
    for index in range(len(tokens) - 1):
        left, _, left_end = tokens[index]
        right, right_start, _ = tokens[index + 1]
        if not (_is_content_token(left) and _is_content_token(right)):
            continue
        if _CJK_RUN.search(normalized[left_end:right_start]):
            continue
        units.add(f"{left} {right}")
    for match in _CJK_RUN.finditer(normalized):
        run = match.group()
        for start in range(len(run)):
            for length in range(2, min(_MAX_CJK_UNIT, len(run) - start) + 1):
                unit = run[start:start + length]
                if not _generic_cjk_unit(unit):
                    units.add(unit)
    return frozenset(units)


def shared_discriminative_unit(left_quote: str, right_quote: str, question: str) -> str | None:
    """返回两段引用共享、且未被问题本身提及的最短区分单元。"""
    shared = discriminative_units(left_quote) & discriminative_units(right_quote)
    if not shared:
        return None
    normalized_question = normalize_text(question)
    candidates = [unit for unit in shared if unit not in normalized_question]
    if not candidates:
        return None
    normalized_left = normalize_text(left_quote)
    return min(candidates, key=lambda unit: (len(unit), normalized_left.find(unit), unit))


def planner_candidate_text(item: RankedEvidence, max_chars: int) -> str:
    """planner 实际看到的候选文本，与 v7 selector 的可见文本规则保持一致。"""
    if isinstance(item.content, str):
        return (item.source_context or item.content)[:max_chars]
    return " ".join(
        part.text for part in item.content if isinstance(part, TextPart)
    )[:max_chars]


@dataclass(frozen=True)
class CandidateView:
    """送交 planner 的一个候选及其可见文本。"""

    index: int
    memory_id: UUID
    user_id: str
    source_group: str
    request_id: str
    source_position: int | None
    granularity: Literal["context", "message"]
    conflict_group_id: UUID | None
    text: str
    evidence: RankedEvidence


@dataclass(frozen=True)
class ValidatedSupport:
    requirement_index: int
    candidate_index: int
    role: SupportRole
    quote: str


@dataclass(frozen=True)
class ValidatedLink:
    left_index: int
    right_index: int
    kind: LinkKind


@dataclass(frozen=True)
class ValidationOutcome:
    valid: bool
    failure_category: GraphFailureCategory
    requirements: tuple[str, ...]
    supports: tuple[ValidatedSupport, ...]
    links: tuple[ValidatedLink, ...]


class GraphContextView(Protocol):
    """校验器可见的、本请求内既有的可证明连接来源。"""

    relation_source_pairs: tuple[tuple[str, str], ...]
    source_context_enabled: bool


def candidate_views(
    pool: Sequence[RankedEvidence], *, max_chars: int
) -> tuple[CandidateView, ...]:
    """按 pool 索引构建候选视图；来源标签与 v7 selector 完全一致。"""
    labels = _source_labels(pool)
    return tuple(
        CandidateView(
            index=index,
            memory_id=item.memory_id,
            user_id=item.user_id,
            source_group=labels[index],
            request_id=item.request_id,
            source_position=item.source_position,
            granularity=item.granularity,
            conflict_group_id=item.conflict_group_id,
            text=planner_candidate_text(item, max_chars),
            evidence=item,
        )
        for index, item in enumerate(pool)
    )


def _link_is_provable(
    kind: LinkKind,
    left: CandidateView,
    right: CandidateView,
    *,
    left_quote: str,
    right_quote: str,
    question: str,
    context: GraphContextView,
    persisted_pairs: frozenset[tuple[str, str]],
) -> bool:
    if kind in {"same_source_context", "confirmation", "correction"}:
        if not context.source_context_enabled:
            return False
        if not left.request_id or left.request_id != right.request_id:
            return False
        if left.source_position is None or right.source_position is None:
            return False
        return abs(left.source_position - right.source_position) == 1
    if kind == "persisted_relation":
        if not left.request_id or not right.request_id:
            return False
        return tuple(sorted((left.request_id, right.request_id))) in persisted_pairs
    return shared_discriminative_unit(left_quote, right_quote, question) is not None


def _bounded_pairs(
    context: GraphContextView,
) -> frozenset[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for pair in context.relation_source_pairs[:MAX_GRAPH_CONTEXT_PAIRS]:
        if len(pair) != 2 or not pair[0] or not pair[1]:
            continue
        pairs.add(tuple(sorted(pair)))  # type: ignore[arg-type]
    return frozenset(pairs)


_INVALID = ValidationOutcome(False, "planner_invalid", (), (), ())


def validate_proposal(
    proposal: "EvidenceGraphProposal",
    *,
    question: str,
    candidates: Sequence[CandidateView],
    context: GraphContextView,
    max_requirements: int = MAX_GRAPH_REQUIREMENTS,
    max_supports: int = MAX_GRAPH_SUPPORTS,
    max_links: int = MAX_GRAPH_LINKS,
) -> ValidationOutcome:
    """结构错误使整份 proposal 失效；个别无法证明的条目只被丢弃。"""
    normalized_question = normalize_text(question)
    if not 1 <= len(proposal.requirements) <= max_requirements:
        return _INVALID
    quotes: list[str] = []
    for requirement in proposal.requirements:
        quote = normalize_text(requirement.question_quote)
        if not quote or quote not in normalized_question:
            return _INVALID
        quotes.append(quote)
    if len(proposal.supports) > max_supports or len(proposal.links) > max_links:
        return _INVALID
    for support in proposal.supports:
        if not 0 <= support.requirement_index < len(proposal.requirements):
            return _INVALID
        if not 0 <= support.candidate_index < len(candidates):
            return _INVALID
    for link in proposal.links:
        if link.left_candidate_index == link.right_candidate_index:
            return _INVALID
        if not 0 <= link.left_candidate_index < len(candidates):
            return _INVALID
        if not 0 <= link.right_candidate_index < len(candidates):
            return _INVALID

    supports: list[ValidatedSupport] = []
    seen_supports: set[tuple[int, int, str, str]] = set()
    for support in proposal.supports:
        view = candidates[support.candidate_index]
        if not grounded_quote(support.candidate_quote, view.text):
            continue
        support_key = (
            support.requirement_index,
            support.candidate_index,
            support.role,
            normalize_text(support.candidate_quote),
        )
        if support_key in seen_supports:
            continue
        seen_supports.add(support_key)
        supports.append(
            ValidatedSupport(
                requirement_index=support.requirement_index,
                candidate_index=support.candidate_index,
                role=support.role,
                quote=support.candidate_quote,
            )
        )

    persisted_pairs = _bounded_pairs(context)
    links: list[ValidatedLink] = []
    seen_links: set[tuple[int, int, str]] = set()
    for link in proposal.links:
        left = candidates[link.left_candidate_index]
        right = candidates[link.right_candidate_index]
        if not grounded_quote(link.left_quote, left.text):
            continue
        if not grounded_quote(link.right_quote, right.text):
            continue
        provable = _link_is_provable(
            link.kind,
            left,
            right,
            left_quote=link.left_quote,
            right_quote=link.right_quote,
            question=question,
            context=context,
            persisted_pairs=persisted_pairs,
        )
        if not provable:
            continue
        low, high = sorted((left.index, right.index))
        link_key = (low, high, link.kind)
        if link_key in seen_links:
            continue
        seen_links.add(link_key)
        links.append(ValidatedLink(left_index=low, right_index=high, kind=link.kind))

    return ValidationOutcome(True, "none", tuple(quotes), tuple(supports), tuple(links))
