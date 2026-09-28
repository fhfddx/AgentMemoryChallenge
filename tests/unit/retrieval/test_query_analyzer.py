"""查询分析单元测试（规则优先，复杂查询才调用 LLM，失败必须可靠降级）。"""

import base64
import io

import httpx
import pytest
from PIL import Image

from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import OpenAICompatibleLLM
from masm.retrieval.query_analyzer import MAX_SUBQUERIES, QueryAnalyzer
from masm.schemas.content import ImageURLPart, TextPart


def _data_url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


_INVALID_BODY = {"choices": [{"message": {"content": "not json"}}]}


def _analyzer(**overrides) -> QueryAnalyzer:
    return QueryAnalyzer(**overrides)


def test_simple_query_uses_rules_without_calling_llm() -> None:
    llm = FakeStructuredLLM()
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("the cat sat on the mat")

    assert llm.requests == []
    assert parsed.text_queries == ("the cat sat on the mat",)
    assert parsed.intent == "fact"


def test_multi_clause_query_produces_at_most_three_subqueries() -> None:
    analyzer = _analyzer()

    parsed = analyzer.parse("first clause. second clause? third clause! fourth clause.")

    assert 1 < len(parsed.text_queries) <= MAX_SUBQUERIES


def test_complex_query_calls_llm_and_is_truncated_to_hard_cap() -> None:
    llm = FakeStructuredLLM([{"text_queries": ["a", "b", "c", "d", "e"], "intent": "temporal"}])
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("what happened before the meeting last week and who attended it")

    assert len(llm.requests) == 1
    assert len(parsed.text_queries) <= MAX_SUBQUERIES


def test_llm_failure_degrades_to_rules() -> None:
    llm = FakeStructuredLLM([RuntimeError("provider down")])
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("what happened before the meeting last week")

    assert parsed.text_queries
    assert len(parsed.text_queries) <= MAX_SUBQUERIES


def test_llm_invalid_json_degrades_to_rules() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_INVALID_BODY)

    llm = OpenAICompatibleLLM(
        model="gpt-4o-mini",
        base_url="https://models.invalid/v1",
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("who was related to the meeting before last week")

    assert parsed.text_queries
    assert len(parsed.text_queries) <= MAX_SUBQUERIES


def test_llm_valid_response_is_used() -> None:
    llm = FakeStructuredLLM(
        [{"text_queries": ["meeting"], "entities": ["alice"], "intent": "relational"}]
    )
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("who was related to the meeting before last week")

    assert parsed.text_queries == ("meeting",)
    assert parsed.entities == ("alice",)
    assert parsed.intent == "relational"


def test_image_query_becomes_visual_query() -> None:
    analyzer = _analyzer()

    parsed = analyzer.parse([ImageURLPart(image_url={"url": _data_url()})])

    assert parsed.intent == "visual"
    assert len(parsed.visual_queries) == 1
    assert parsed.text_queries == ()


def test_mixed_query_keeps_order_and_intent() -> None:
    analyzer = _analyzer()

    parsed = analyzer.parse(
        [TextPart(text="a red square"), ImageURLPart(image_url={"url": _data_url()})]
    )

    assert parsed.text_queries == ("a red square",)
    assert len(parsed.visual_queries) == 1
    assert parsed.intent == "visual"


@pytest.mark.parametrize(
    "empty_output",
    [
        {},
        {"text_queries": []},
        {"text_queries": ["", "  "]},
    ],
)
def test_empty_llm_output_falls_back_to_original_query(empty_output: dict) -> None:
    """LLM 输出语义不可用时，必须用原始查询做规则降级而不是丢失查询。"""
    query = "what happened before the meeting last week and who attended it"
    analyzer = _analyzer(llm=FakeStructuredLLM([empty_output]))

    parsed = analyzer.parse(query)

    assert parsed.text_queries
    assert len(parsed.text_queries) <= MAX_SUBQUERIES
    assert query.split()[0] in " ".join(parsed.text_queries)


@pytest.mark.parametrize("bad_intent", ["nonsense", "", "ANSWER"])
def test_invalid_intent_is_never_carried_over(bad_intent: str) -> None:
    llm = FakeStructuredLLM([{"text_queries": ["meeting"], "intent": bad_intent}])
    analyzer = _analyzer(llm=llm)

    parsed = analyzer.parse("who was related to the meeting before last week")

    assert parsed.intent in {"fact", "temporal", "relational", "visual"}


@pytest.mark.parametrize("configured", [4, 100, 10_000])
def test_max_subqueries_cannot_exceed_hard_cap(configured: int) -> None:
    analyzer = _analyzer(max_subqueries=configured)

    assert analyzer.max_subqueries == MAX_SUBQUERIES


def test_negative_max_subqueries_is_rejected() -> None:
    with pytest.raises(ValueError):
        _analyzer(max_subqueries=-1)
