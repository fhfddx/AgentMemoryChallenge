"""空历史文本融合智能体的离线契约测试。"""

from datetime import UTC, datetime

import pytest

from masm.agents.fused_text import FusedTextAgent, has_one_supported_date
from masm.agents.temporal import AgentOutputError
from masm.providers.fakes import FakeStructuredLLM
from masm.schemas.agents import (
    FusedTextResult,
    PerceptionResult,
    TimePrecision,
)
from masm.schemas.content import TextPart


def test_extract_preserves_order_and_event_time() -> None:
    event_time = datetime(2026, 10, 3, tzinfo=UTC)
    llm = FakeStructuredLLM([
        FusedTextResult(
            perception=PerceptionResult(modality="text", keywords=("meeting",)),
            event_time=event_time,
            time_precision=TimePrecision.DAY,
            time_evidence="2026-10-03",
        )
    ])
    agent = FusedTextAgent(llm)

    result = agent.extract([TextPart(text="first"), TextPart(text="2026-10-03 meeting")])

    assert result.event_time == event_time
    assert result.time_precision is TimePrecision.DAY
    assert [part.text for part in llm.requests[0].content or ()] == [
        "first", "2026-10-03 meeting",
    ]
    assert llm.requests[0].prompt_version == "fused-text-v2"


def test_mixed_format_second_date_is_not_fusion_eligible() -> None:
    assert has_one_supported_date([
        TextPart(text="Meeting moved from October 4, 2026 to 2026-10-03"),
    ]) is False


@pytest.mark.parametrize("date_text", ("2026-10-3", "2026-10/03", "2026/10-03"))
def test_abbreviated_or_mixed_separator_date_is_not_fusion_eligible(date_text: str) -> None:
    assert has_one_supported_date([TextPart(text=f"Meeting on {date_text}")]) is False


def test_prompt_requires_observable_evidence_and_no_invented_relation() -> None:
    prompt = FusedTextAgent(FakeStructuredLLM()).prompt.lower()

    assert "do not infer" in prompt
    assert "evidence" in prompt
    assert "time_evidence" in prompt
    assert "unknown" in prompt


def test_rejects_time_evidence_not_in_original_text() -> None:
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(),
        event_time=datetime(2026, 10, 3, tzinfo=UTC),
        time_precision=TimePrecision.DAY,
        time_evidence="2026-10-03",
    )])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="meeting")])


def test_rejects_calendar_digits_embedded_in_identifier() -> None:
    """模型不能把合成标识符末尾的 20261004 写成事件日期。"""
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(),
        event_time=datetime(2026, 10, 4, tzinfo=UTC),
        time_precision=TimePrecision.DAY,
        time_evidence="Saffron-fwlat6d20261004",
    )])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([
            TextPart(text="Use the Saffron-fwlat6d20261004 folder."),
        ])


def test_rejects_normalized_day_that_disagrees_with_evidence() -> None:
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(),
        event_time=datetime(2026, 10, 4, tzinfo=UTC),
        time_precision=TimePrecision.DAY,
        time_evidence="2026-10-03",
    )])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="Meeting on 2026-10-03.")])


def test_rejects_missing_time_for_standalone_date() -> None:
    llm = FakeStructuredLLM([FusedTextResult(perception=PerceptionResult())])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="Meeting on 2026-10-03.")])


def test_accepts_date_adjacent_to_chinese_sentence() -> None:
    event_time = datetime(2026, 10, 3, tzinfo=UTC)
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(), event_time=event_time,
        time_precision=TimePrecision.DAY, time_evidence="2026-10-03",
    )])

    result = FusedTextAgent(llm).extract([TextPart(text="会议于2026-10-03举行。")])

    assert result.event_time == event_time


def test_rejects_missing_time_for_date_adjacent_to_chinese_sentence() -> None:
    llm = FakeStructuredLLM([FusedTextResult(perception=PerceptionResult())])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="会议于2026-10-03举行。")])


def test_rejects_time_without_precision() -> None:
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(),
        event_time=datetime(2026, 10, 3, tzinfo=UTC),
        time_evidence="2026-10-03",
    )])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="2026-10-03 meeting")])


def test_rejects_non_text_perception() -> None:
    llm = FakeStructuredLLM([FusedTextResult(
        perception=PerceptionResult(modality="image"),
    )])

    with pytest.raises(AgentOutputError):
        FusedTextAgent(llm).extract([TextPart(text="meeting")])
