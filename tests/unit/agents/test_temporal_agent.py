"""时序关系智能体单元测试（只使用 Fake Provider，不访问付费 API）。"""

from uuid import uuid4

import pytest

from masm.agents.temporal import AgentOutputError, TemporalRelationAgent
from masm.providers.fakes import FakeStructuredLLM
from masm.schemas.agents import (
    ObservedEvent,
    PerceptionResult,
    RelationKind,
    RelationSuggestion,
    TemporalRelationResult,
)
from masm.storage.types import MemoryCandidate


def _candidate(index: int) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content=f"memory {index}", score=0.0
    )


def _perception() -> PerceptionResult:
    return PerceptionResult(
        events=(
            ObservedEvent(
                description="walked the dog",
                confidence=0.9,
                evidence="I walked the dog",
            ),
        ),
        language="en",
    )


def _relation(target: object, **overrides: object) -> RelationSuggestion:
    payload: dict = {
        "kind": RelationKind.SUPPLEMENTS,
        "target_memory_id": target,
        "confidence": 0.7,
        "evidence": "walked the dog",
    }
    payload.update(overrides)
    return RelationSuggestion(**payload)


def test_analyze_returns_schema_object() -> None:
    candidate = _candidate(0)
    llm = FakeStructuredLLM(
        [TemporalRelationResult(relations=(_relation(candidate.memory_id),))]
    )
    agent = TemporalRelationAgent(llm)

    result = agent.analyze(_perception(), [candidate])

    assert isinstance(result, TemporalRelationResult)
    assert result.relations[0].target_memory_id == candidate.memory_id


def test_history_is_bounded_by_configuration() -> None:
    """历史候选数量必须受配置上限约束。"""
    history = [_candidate(index) for index in range(20)]
    llm = FakeStructuredLLM([TemporalRelationResult()])
    agent = TemporalRelationAgent(llm, max_history=5)

    agent.analyze(_perception(), history)

    payload_history = llm.requests[0].payload["history"]
    assert len(payload_history) == 5
    assert payload_history[0]["content"] == "memory 0"


def test_relation_to_unknown_memory_is_rejected() -> None:
    """关系只能指向本次提供的同用户历史候选。"""
    candidate = _candidate(0)
    llm = FakeStructuredLLM(
        [TemporalRelationResult(relations=(_relation(uuid4()),))]
    )
    agent = TemporalRelationAgent(llm)

    with pytest.raises(AgentOutputError):
        agent.analyze(_perception(), [candidate])


def test_event_order_must_reference_perceived_events() -> None:
    llm = FakeStructuredLLM(
        [TemporalRelationResult(event_order=("an event nobody observed",))]
    )
    agent = TemporalRelationAgent(llm)

    with pytest.raises(AgentOutputError):
        agent.analyze(_perception(), [])


def test_event_order_accepts_perceived_events() -> None:
    llm = FakeStructuredLLM([TemporalRelationResult(event_order=("walked the dog",))])
    agent = TemporalRelationAgent(llm)

    result = agent.analyze(_perception(), [])

    assert result.event_order == ("walked the dog",)


def test_negative_max_history_is_rejected() -> None:
    with pytest.raises(ValueError):
        TemporalRelationAgent(FakeStructuredLLM(), max_history=-1)


def test_analyze_records_model_and_prompt_version() -> None:
    llm = FakeStructuredLLM([TemporalRelationResult()], model="gpt-4o-mini")
    agent = TemporalRelationAgent(llm)

    agent.analyze(_perception(), [])

    request = llm.requests[0]
    assert request.model == "gpt-4o-mini"
    assert request.prompt_version == "v1"
    assert request.prompt == agent.prompt


def test_agent_rejects_repository_injection() -> None:
    """智能体只接受 Provider 与配置，不接收 Repository 或数据库会话。"""
    llm = FakeStructuredLLM()

    with pytest.raises(TypeError):
        TemporalRelationAgent(llm, repository=object())


def test_prompt_forbids_unobservable_inference_and_invented_ids() -> None:
    prompt = TemporalRelationAgent(FakeStructuredLLM()).prompt.lower()

    assert "do not infer" in prompt
    assert "not directly observable" in prompt
    assert "do not invent memory ids" in prompt
