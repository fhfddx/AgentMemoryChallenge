"""Deterministic grounding rules for the opt-in query-time evidence graph.

The planner only proposes; this module decides what is provable. Quotes are matched against
the exact text the planner saw, and a candidate pair may only be connected by adjacent
same-source context, a persisted relation proven by the expansion sidecar, or an explicit
discriminative mention that is absent from the question. Generic relation nouns, stopwords,
short numbers and punctuation never connect anything, so an unprovable bridge fails closed.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
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
# 结构失败的原因：固定枚举，只用于诊断（不含任何模型文本）。
PlannerFailureReason = Literal[
    "none",
    "schema_invalid",
    "planner_unavailable",
    "internal_error",
    "candidate_scope_mismatch",
    "empty_requirements",
    "requirement_count_exceeded",
    "requirement_quote_not_grounded",
    "requirement_quote_reordered",
    "requirement_quote_paraphrased",
    "support_count_exceeded",
    "support_index_out_of_range",
    "link_count_exceeded",
    "link_index_out_of_range",
    "link_self_reference",
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
# 来源上下文的 JSON 结构键与 JSON 字面量：它们在不同来源之间必然重复，绝不构成区分单元。
_STRUCTURAL_TOKENS = frozenset(
    {
        "assistant", "false", "matched", "message", "messages", "null", "omitted",
        "position", "positions", "role", "text", "timestamp", "true", "user",
    }
)

_LATIN_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_DIGITS = re.compile(r"[0-9]+")
# 原始问题里的"名字样"记号：允许词内大写、数字、连字符与下划线。
# 疑问词与填充词：出现在问题里但不会出现在陈述记忆里，模板对齐前必须剔除。
_QUESTION_WORDS = frozenset(
    {
        "what", "who", "when", "where", "which", "why", "how", "is", "are", "was", "were",
        "do", "does", "did", "can", "could", "should", "would", "will", "please", "tell",
        "remind", "me", "my", "you", "your", "and", "about",
    }
)
_NAME_LIKE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
_NAME_PART = re.compile(r"[A-Za-z0-9]+")
# 句中大写但通常不是"主体名"的词：据此拒答只会误伤可回答问题。
_NAME_DENYLIST = frozenset(
    {
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "i", "what", "when", "where",
        "which", "who", "why", "how", "the", "a", "an", "my", "our", "please",
    }
)
_NAME_PART_MIN_CHARS = 4
_NAME_STEM_MIN_CHARS = 5


def normalize_text(text: str) -> str:
    """NFKC + casefold + 空白折叠；绝不删除否定词或改变语义。"""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def grounded_quote(quote: str, text: str) -> bool:
    """引用必须非空且是 planner 实际所见文本的归一化精确子串。"""
    normalized = normalize_text(quote)
    return bool(normalized) and normalized in normalize_text(text)


_NON_WORD = re.compile(r"[\W_]+")
# 子句/绑定边界标记：只有问题带绑定结构时才收紧“完整”的含义。
# 单子句的省略回答（"What color bicycle did I buy?" ← "No, I bought the red one."）必须继续被接受。
_CLAUSE_MARKERS = (" that ", " which ", " who ", " whom ", " whose ")
_BINDING_PREPOSITIONS = (" for ", " of ", " with ", " about ", " regarding ")
_CLAUSE_PUNCTUATION = (",", ";", "，", "；", "、")


def quote_shape(text: str) -> str:
    """去掉标点后的形状：容忍标点差异，但绝不放行改写或改序。"""
    return " ".join(_NON_WORD.sub(" ", normalize_text(text)).split())


def question_has_clause_marker(question: str) -> bool:
    """问题是否含绑定结构标记：子句标点、关系代词，或绑定介词短语。"""
    normalized = normalize_text(question)
    if any(marker in normalized for marker in _CLAUSE_PUNCTUATION):
        return True
    padded = f" {normalized} "
    if any(marker in padded for marker in _CLAUSE_MARKERS):
        return True
    return any(marker in padded for marker in _BINDING_PREPOSITIONS)


def _grounding_mismatch_reason(
    quote: str, normalized_question: str
) -> PlannerFailureReason:
    """区分“同词不同序”和“改写用词”，让契约问题可定位且不泄露文本。"""
    quote_tokens = Counter(quote_shape(quote).split())
    question_tokens = Counter(quote_shape(normalized_question).split())
    if quote_tokens and quote_tokens == question_tokens:
        return "requirement_quote_reordered"
    return "requirement_quote_paraphrased"


def _is_content_token(token: str) -> bool:
    """既非停用词/泛化名词/结构键，也非过短数字：可以作为区分单元的一部分。"""
    if token in _STOPWORDS or token in _GENERIC_LATIN_UNITS or token in _STRUCTURAL_TOKENS:
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


def _is_name_part(part: str) -> bool:
    """判定用的名字部分：字母部分需 >=5 字符，纯数字部分 2 位即可（如 item-47 的 47）。"""

    if not part:
        return False
    if part.isdigit():
        return len(part) >= 2
    return len(part) >= _NAME_STEM_MIN_CHARS

def _part_is_name_shaped(part: str, *, sentence_initial: bool = False) -> bool:
    """只有"标识符"才算名字：含数字或词内大写，且不在小词表内。

    普通的首字母大写（Alice、Porto）不算：官方 direct 类问题普遍用名字指代用户或实体，而记忆多以第一
    人称书写，那个名字在库里根本不存在，据此拒答会把 direct recall 清零（已由官方运行证实）。
    句首大写只是排版信号，仅为接口一致性保留该参数。
    """

    del sentence_initial
    if part.casefold() in _NAME_DENYLIST:
        return False
    return any(char.isdigit() for char in part) or any(
        char.isupper() for char in part[1:]
    )


def name_tokens(question: str) -> frozenset[str]:
    """问题里的"名字样"记号（含数字、词内大写或句中首字母大写；排除句首排版与日历/代词）。

    缺失判定必须交给"全库"级别：只在候选池里缺失只说明排序，不代表库里没有这个主体。
    """

    payload = unicodedata.normalize("NFKC", question)
    tokens: set[str] = set()
    for match in _NAME_LIKE.finditer(payload):
        token = match.group()
        if len(token) < _NAME_PART_MIN_CHARS:
            continue
        sentence_initial = match.start() == 0 or payload[: match.start()].rstrip()[-1:] in {
            ".",
            "?",
            "!",
            ";",
            ":",
        }
        if not _part_is_name_shaped(token, sentence_initial=sentence_initial):
            continue
        parts = [
            part
            for part in _NAME_PART.findall(token)
            if _is_name_part(part)
            and _part_is_name_shaped(part, sentence_initial=sentence_initial)
        ]
        if not parts:
            continue
        tokens.add(normalize_text(token))
    return frozenset(tokens)


def absent_name_tokens(question: str, pool_texts: Sequence[str]) -> frozenset[str]:
    """问题中的"名字样"记号若在整个候选池中从未出现，则池内证据回答不了这个问题。

    只看名字（含数字、词内大写或句中首字母大写）：普通内容词换一种说法就可能不在池里，据此拒答会
    误伤可回答问题；而一个问题点名的主体若在池里根本不存在，任何"同属性、不同主体"的证据都答非所问。
    句首大写只是排版（例如 "Follow ..." 这类祈使句），不构成名字信号。
    """

    normalized_pool = " ".join(normalize_text(text) for text in pool_texts)
    payload = unicodedata.normalize("NFKC", question)
    absent: set[str] = set()
    for match in _NAME_LIKE.finditer(payload):
        token = match.group()
        if len(token) < _NAME_PART_MIN_CHARS:
            continue
        sentence_initial = match.start() == 0 or payload[: match.start()].rstrip()[-1:] in {
            ".",
            "?",
            "!",
            ";",
            ":",
        }
        if not _part_is_name_shaped(token, sentence_initial=sentence_initial):
            continue
        # 只看名字样的部分：共享的修饰词（例如都含 office）不得把缺失的主体说成存在；
        # 任一名模样部分（>=5 字符）在整个池中缺失，即判定这个名字未被存过。
        name_parts = [
            normalize_text(part)
            for part in _NAME_PART.findall(token)
            if _is_name_part(part)
            and _part_is_name_shaped(part, sentence_initial=sentence_initial)
        ]
        if not name_parts:
            continue
        if all(part in normalized_pool for part in name_parts):
            continue
        absent.add(normalize_text(token))
    return frozenset(absent)


def frame_slot_conflict(question: str, memory_texts: Sequence[str]) -> bool:
    """库里是否存在"同框架、不同主体"的记忆：问题的非名字内容词大多出现在同一条记忆里。

    这是"该拒答"的判据：例如库里只有 item-42 的口令，问题问 item-47 的口令。用户自己名字缺失
    不属于这种情况——库里没有"同框架、另一个名字"的记忆，因此不会被误判成不可答。
    """

    names = name_tokens(question)
    if not names:
        return False
    lowered = normalize_text(question)
    for name in names:
        lowered = lowered.replace(name, " ")
    frame = {
        unit
        for unit in discriminative_units(lowered)
        if not any(unit in name for name in names)
    }
    if len(frame) < 2:
        return False
    needed = max(2, (len(frame) + 1) // 2)
    for text in memory_texts:
        units = discriminative_units(text)
        if len(frame & units) >= needed:
            return True
    return False

def slot_template_conflict(question: str, memory_texts: Sequence[str]) -> bool:
    """库里是否存在"同一模板、不同主体"的记忆：名字位置通配，其余词序与相邻位置必须对齐。

    这是"该拒答"的强判据。仅是"内容词重叠"不够：direct 问题与它的来源记忆也大量重叠，但两者的
    模板不同（问题是 "what did <名> say about ..."，记忆是 "my sister <名> works at ..."）。
    """

    names = name_tokens(question)
    if not names:
        return False
    tokens = [
        token
        for token in normalize_text(question).split()
        if _strip_possessive(token.strip(",.?!;:\'")) not in _QUESTION_WORDS
    ]
    if len(tokens) < 4:
        return False
    name_parts = {part for name in names for part in name.split()}
    pattern: list[str | None] = []
    for token in tokens:
        bare = _strip_possessive(token.strip(",.?!;:\'"))
        pattern.append(None if bare in name_parts else bare)
    slots = sum(1 for item in pattern if item is None)
    if slots == 0 or len(pattern) - slots < 3:
        return False
    for text in memory_texts:
        memory_tokens = [
            _strip_possessive(token.strip(",.?!;:\'"))
            for token in normalize_text(text).split()
        ]
        width = len(pattern)
        for start in range(0, len(memory_tokens) - width + 1):
            window = memory_tokens[start : start + width]
            if all(
                expected is None or expected == actual
                for expected, actual in zip(pattern, window, strict=True)
            ):
                return True
    return False


def _strip_possessive(unit: str) -> str:
    """把 "sister's" 归一到 "sister"（仅用于模板对齐比较）。"""

    return unit.replace("'s", "").replace("\u2019s", "").replace("'", "").replace("\u2019", "")

_RELATION_WORDS = frozenset(
    {
        "sister", "brother", "mother", "father", "mom", "dad", "wife", "husband",
        "partner", "son", "daughter", "friend", "boss", "manager", "colleague",
        "coworker", "neighbor", "neighbour", "landlord", "doctor", "dentist",
        "teacher", "cousin", "aunt", "uncle", "grandmother", "grandfather",
    }
)
_RELATION_PATTERN = "|".join(sorted(_RELATION_WORDS, key=len, reverse=True))
# 关系前缀名：支持 "my sister Maya" 以及 "the sister named Maya"、"sister called Maya"。
_RELATION_NAME = re.compile(
    r"\b(?i:"
    + _RELATION_PATTERN
    + r")\s+(?:named\s+|called\s+)?([A-Z][A-Za-z0-9'\u2019_-]{2,})"
)
# 所有格前置名：Maya's sister 同样指向一个具体关系人。
_POSSESSIVE_NAME = re.compile(
    r"\b([A-Z][A-Za-z0-9'-]{2,})['\u2019]s\s+(?i:" + _RELATION_PATTERN + r")\b"
)


def relation_capitalized_names(question: str) -> frozenset[str]:
    """提取"关系词 + 首字母大写词"里的名字（如 "my sister Maya"），用于关系人缺失判定。

    与 ``name_tokens`` 不同：这里接受 3 个字母以上的短名字，因为关系前缀本身就是强信号；用户自己
    的名字不会以这种形式出现，因此不会误伤以用户为主语的 direct 问题。
    """

    payload = unicodedata.normalize("NFKC", question)
    found: set[str] = set()
    for pattern in (_RELATION_NAME, _POSSESSIVE_NAME):
        for match in pattern.finditer(payload):
            name = match.group(1)
            if name.casefold() in _NAME_DENYLIST:
                continue
            found.add(normalize_text(name))
    return frozenset(found)

def shared_discriminative_unit(
    left_quote: str,
    right_quote: str,
    question: str,
    *,
    unit_frequencies: Mapping[str, int] | None = None,
    max_frequency: int | None = None,
) -> str | None:
    """返回两段引用共享、且未被问题本身提及的最短区分单元。

    ``unit_frequencies``/``max_frequency`` 可进一步要求该单元在本次候选池内足够稀有：
    重复措辞（例如每个来源都写的“Grounded support”）不得把无关来源连起来。
    """
    shared = discriminative_units(left_quote) & discriminative_units(right_quote)
    if not shared:
        return None
    normalized_question = normalize_text(question)
    candidates = [unit for unit in shared if unit not in normalized_question]
    if unit_frequencies is not None and max_frequency is not None:
        candidates = [
            unit for unit in candidates if unit_frequencies.get(unit, 0) <= max_frequency
        ]
    if not candidates:
        return None
    normalized_left = normalize_text(left_quote)
    return min(candidates, key=lambda unit: (len(unit), normalized_left.find(unit), unit))


def unit_frequencies(pool: Sequence["CandidateView"]) -> dict[str, int]:
    """统计每个区分单元在候选池中出现在多少个候选里（文档频率）。"""
    counts: dict[str, int] = {}
    for view in pool:
        for unit in discriminative_units(view.text):
            counts[unit] = counts.get(unit, 0) + 1
    return counts


def _is_cjk_unit(unit: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff"
        for character in unit
    )


def attested_units(text: str, frequencies: Mapping[str, int]) -> frozenset[str]:
    """完整性判定使用的单元：拉丁单元照收，CJK 单元必须在本请求候选池中被实际证实。

    未分词的 CJK 子串会产生跨词边界的伪单元（例如“城市举办”里的“市举”）；只有当它真的
    出现在某个候选里时，才允许它成为“问题里还没落地”的硬性要求。
    """
    return frozenset(
        unit
        for unit in discriminative_units(text)
        if not _is_cjk_unit(unit) or frequencies.get(unit, 0) >= 1
    )


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
    reason: PlannerFailureReason = "none"


class GraphContextView(Protocol):
    """校验器可见的、本请求内既有的可证明连接来源。"""

    @property
    def relation_source_pairs(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def source_context_enabled(self) -> bool: ...


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


def bounded_persisted_pairs(
    context: GraphContextView,
) -> frozenset[tuple[str, str]]:
    """本请求可见的、去重且双向归一的侧车关系来源对（上限见 MAX_GRAPH_CONTEXT_PAIRS）。"""
    pairs: set[tuple[str, str]] = set()
    for pair in context.relation_source_pairs[:MAX_GRAPH_CONTEXT_PAIRS]:
        if len(pair) != 2 or not pair[0] or not pair[1]:
            continue
        if pair[0] == pair[1]:
            continue
        pairs.add(tuple(sorted(pair)))  # type: ignore[arg-type]
    return frozenset(pairs)


def _bounded_pairs(
    context: GraphContextView,
) -> frozenset[tuple[str, str]]:
    return bounded_persisted_pairs(context)


def _invalid(reason: PlannerFailureReason) -> ValidationOutcome:
    """结构失败：整份 proposal 失效，同时给出固定枚举的原因用于诊断。"""
    return ValidationOutcome(False, "planner_invalid", (), (), (), reason)


def _adjacent_same_source(
    left: CandidateView, right: CandidateView, *, source_context_enabled: bool
) -> bool:
    if not source_context_enabled:
        return False
    if not left.request_id or left.request_id != right.request_id:
        return False
    if left.source_position is None or right.source_position is None:
        return False
    return abs(left.source_position - right.source_position) == 1


def derived_links(
    question: str,
    candidates: Sequence[CandidateView],
    *,
    source_context_enabled: bool,
    persisted_pairs: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[ValidatedLink, ...]:
    """确定性代码直接证明的三类连接，不依赖模型是否愿意提出链接。

    1. 同来源相邻上下文：同一非空来源、位置相差 1，且本请求可用来源上下文（规格连接类型 1）；
    2. 跨来源显式 mention：两组候选正文共享问题中不存在的区分单元（规格连接类型 3），
       且该单元在本次候选池中足够稀有（文档频率不超过 2），避免重复措辞连成一片；
    3. 侧车已存关系：两组候选的来源构成 `relation_source_pairs` 中的一对（规格连接类型 2）。
       已存关系是事实，不需要模型先提出；没有该对时跨来源候选绝不会被连起来。

    模型提议的链接仍照旧独立校验；这里只是不把可证明的连接交给模型决定。
    """
    frequencies = unit_frequencies(candidates)
    links: list[ValidatedLink] = []
    for position, left in enumerate(candidates):
        for right in candidates[position + 1:]:
            if _adjacent_same_source(left, right, source_context_enabled=source_context_enabled):
                links.append(
                    ValidatedLink(
                        left_index=left.index,
                        right_index=right.index,
                        kind="same_source_context",
                    )
                )
                continue
            if left.source_group == right.source_group:
                continue
            if (
                left.request_id
                and right.request_id
                and tuple(sorted((left.request_id, right.request_id))) in persisted_pairs
            ):
                links.append(
                    ValidatedLink(
                        left_index=left.index,
                        right_index=right.index,
                        kind="persisted_relation",
                    )
                )
                continue
            if (
                shared_discriminative_unit(
                    left.text,
                    right.text,
                    question,
                    unit_frequencies=frequencies,
                    max_frequency=2,
                )
                is not None
            ):
                links.append(
                    ValidatedLink(
                        left_index=left.index,
                        right_index=right.index,
                        kind="shared_explicit_mention",
                    )
                )
    return tuple(links)


def validate_proposal(
    proposal: "EvidenceGraphProposal",
    *,
    question: str,
    candidates: Sequence[CandidateView],
    context: GraphContextView,
    expected_user_id: str,
    max_requirements: int = MAX_GRAPH_REQUIREMENTS,
    max_supports: int = MAX_GRAPH_SUPPORTS,
    max_links: int = MAX_GRAPH_LINKS,
) -> ValidationOutcome:
    """结构错误使整份 proposal 失效；个别无法证明的条目只被丢弃。"""
    if any(candidate.user_id != expected_user_id for candidate in candidates):
        return _invalid("candidate_scope_mismatch")
    normalized_question = normalize_text(question)
    if not proposal.requirements:
        return _invalid("empty_requirements")
    if len(proposal.requirements) > max_requirements:
        return _invalid("requirement_count_exceeded")
    quotes: list[str] = []
    shape_question = quote_shape(question)
    for requirement in proposal.requirements:
        quote = normalize_text(requirement.question_quote)
        if not quote:
            return _invalid("requirement_quote_not_grounded")
        if quote not in normalized_question:
            # 标点差异（逗号、问号、连字符写法）不改变“引文即问题原文”，但改写或改序仍然拒绝。
            shaped = quote_shape(quote)
            if not shaped or shaped not in shape_question:
                return _invalid(_grounding_mismatch_reason(quote, normalized_question))
        quotes.append(quote)
    if len(proposal.supports) > max_supports:
        return _invalid("support_count_exceeded")
    if len(proposal.links) > max_links:
        return _invalid("link_count_exceeded")
    for support in proposal.supports:
        if not 0 <= support.requirement_index < len(proposal.requirements):
            return _invalid("support_index_out_of_range")
        if not 0 <= support.candidate_index < len(candidates):
            return _invalid("support_index_out_of_range")
    for link in proposal.links:
        if link.left_candidate_index == link.right_candidate_index:
            return _invalid("link_self_reference")
        if not 0 <= link.left_candidate_index < len(candidates):
            return _invalid("link_index_out_of_range")
        if not 0 <= link.right_candidate_index < len(candidates):
            return _invalid("link_index_out_of_range")

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
