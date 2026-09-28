"""查询分析：简单查询用确定性规则，复杂查询才调用 LLM，失败必须可靠降级。"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from masm.providers.llm import ModelRequest, StructuredLLM
from masm.retrieval.baseline import ParsedQuery
from masm.schemas.content import ContentPart, ImageURLPart, TextPart
from masm.storage.assets import decode_image_data_url

PROMPT_VERSION = "v1"

# 子查询硬上限（设计规范 8.1：最多三个子查询）。
MAX_SUBQUERIES = 3

# 需要多子查询/关系与时间推理的复杂度标记。
_COMPLEX_MARKERS = (
    "before",
    "after",
    "when",
    "during",
    "while",
    "last",
    "next",
    "yesterday",
    "tomorrow",
    "who",
    "whose",
    "which",
    "related",
    "relationship",
    "about",
    "之前",
    "之后",
    "以前",
    "以后",
    "什么时候",
    "谁",
    "关系",
    "关于",
    "上周",
    "下周",
    "昨天",
    "明天",
)

_CLAUSE_SEPARATORS = "。！？!?;；\n"

_SYSTEM_PROMPT = (
    "You are the query-analysis stage of a multimodal memory system.\n"
    "Split the user's retrieval request into at most three focused sub-queries.\n"
    "Rules (mandatory):\n"
    "- Return at most three text_queries; never more.\n"
    "- Do not answer the request and do not invent facts.\n"
    "- Keep entities, time constraints, location constraints and relation hints that appear\n"
    "  literally in the request; use empty lists otherwise.\n"
    "- intent must be one of: fact, temporal, relational, visual.\n"
    "- Output JSON only, with no surrounding commentary.\n"
)


class QueryAnalysis(BaseModel):
    """查询分析的 LLM 输出 Schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text_queries: tuple[str, ...] = Field(default=(), max_length=8)
    entities: tuple[str, ...] = ()
    time_constraints: tuple[str, ...] = ()
    location_constraints: tuple[str, ...] = ()
    relation_hints: tuple[str, ...] = ()
    intent: str = "fact"


def resolve_max_subqueries(value: int | None = None) -> int:
    """把子查询上限约束到 ``0..MAX_SUBQUERIES``：负数拒绝，越界安全截断。"""
    if value is None:
        return MAX_SUBQUERIES
    if value < 0:
        raise ValueError("max_subqueries 必须为非负整数")
    return min(value, MAX_SUBQUERIES)


class QueryAnalyzer:
    """查询分析器：规则优先，只有复杂查询才调用 LLM。"""

    def __init__(
        self,
        *,
        llm: StructuredLLM | None = None,
        model: str | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_image_bytes: int = 10 * 1024 * 1024,
        max_subqueries: int | None = None,
    ) -> None:
        self._llm = llm
        self._model = model or (llm.model if llm is not None else "rule-based")
        self._prompt_version = prompt_version
        self._max_image_bytes = max_image_bytes
        self._max_subqueries = resolve_max_subqueries(max_subqueries)

    @property
    def max_subqueries(self) -> int:
        """实际生效的子查询上限。"""
        return self._max_subqueries

    def parse(self, query: str | Sequence[ContentPart]) -> ParsedQuery:
        """解析查询；LLM 失败/超时/非法 JSON/Schema 错误时回落到确定性规则。"""
        texts, images = self._split(query)
        if not texts:
            return ParsedQuery(visual_queries=tuple(images), intent="visual" if images else "fact")

        joined = " ".join(texts).strip()
        if images:
            return self._rule_based(joined, images, intent="visual")
        if self._llm is None or not _is_complex(joined):
            return self._rule_based(joined, images)
        try:
            analysis = self._llm.complete_json(
                self._request(joined), QueryAnalysis
            )
        except Exception:
            # 任何 Provider/解析/校验失败都可靠降级为规则结果。
            return self._rule_based(joined, images)
        return self._from_analysis(analysis, images)

    def _request(self, text: str) -> ModelRequest:
        return ModelRequest(
            prompt=_SYSTEM_PROMPT,
            payload={"query": text},
            model=self._model,
            prompt_version=self._prompt_version,
        )

    def _split(self, query: str | Sequence[ContentPart]) -> tuple[list[str], list[bytes]]:
        if isinstance(query, str):
            return ([query.strip()] if query.strip() else []), []
        texts: list[str] = []
        images: list[bytes] = []
        for part in query:
            if isinstance(part, TextPart):
                texts.append(part.text)
            elif isinstance(part, ImageURLPart):
                images.append(
                    decode_image_data_url(part.image_url.url, self._max_image_bytes).data
                )
        return texts, images

    def _from_analysis(self, analysis: QueryAnalysis, images: Sequence[bytes]) -> ParsedQuery:
        subqueries = tuple(
            text for text in analysis.text_queries if text.strip()
        )[: self._max_subqueries]
        if not subqueries:
            return self._rule_based("", images, intent=analysis.intent)
        return ParsedQuery(
            text_queries=subqueries,
            visual_queries=tuple(images),
            entities=_bounded(analysis.entities),
            time_constraints=_bounded(analysis.time_constraints),
            location_constraints=_bounded(analysis.location_constraints),
            relation_hints=_bounded(analysis.relation_hints),
            intent="visual" if images else _intent(analysis.intent),
        )

    def _rule_based(
        self, text: str, images: Sequence[bytes], *, intent: str | None = None
    ) -> ParsedQuery:
        subqueries = _split_clauses(text)[: self._max_subqueries]
        return ParsedQuery(
            text_queries=tuple(subqueries),
            visual_queries=tuple(images),
            relation_hints=_markers_in(text, _RELATION_MARKERS),
            time_constraints=_markers_in(text, _TIME_MARKERS),
            intent=intent or ("visual" if images else _rule_intent(text)),
        )


_RELATION_MARKERS = ("who", "whose", "related", "relationship", "谁", "关系")
_TIME_MARKERS = (
    "before", "after", "when", "last", "next", "yesterday", "tomorrow",
    "之前", "之后", "上周", "下周", "昨天", "明天",
)


def _bounded(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(value for value in values if value.strip())[:8]


def _intent(value: str) -> str:
    return value if value in {"fact", "temporal", "relational", "visual"} else "fact"


def _is_complex(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _COMPLEX_MARKERS):
        return True
    return len(_split_clauses(text)) > 1


def _split_clauses(text: str) -> list[str]:
    """按句子与分句边界切分，并去掉空片段。"""
    parts: list[str] = []
    current: list[str] = []
    for character in text:
        if character in _CLAUSE_SEPARATORS:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _rule_intent(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _RELATION_MARKERS):
        return "relational"
    if any(marker in lowered for marker in _TIME_MARKERS):
        return "temporal"
    return "fact"


def _markers_in(text: str, markers: Sequence[str]) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(marker for marker in markers if marker in lowered)[:8]
