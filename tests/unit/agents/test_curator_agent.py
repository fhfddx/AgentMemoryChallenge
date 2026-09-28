"""记忆管理智能体单元测试（只使用 Fake Provider，不访问付费 API）。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from masm.agents import MAX_HISTORY
from masm.agents.curator import MemoryCuratorAgent
from masm.providers.fakes import FakeStructuredLLM
from masm.schemas.agents import (
    ActionKind,
    CuratorAction,
    CuratorDecision,
)
from masm.storage.types import MemoryCandidate, MemoryDraft

_FORBIDDEN_PROMPT_MARKERS = (
    "answer:",
    "correct answer",
    "example question",
    "expected output",
    "benchmark",
    "q:",
)


def _draft() -> MemoryDraft:
    return MemoryDraft(summary="a new memory", original_text="a new memory")


def _candidate(index: int) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content=f"memory {index}", score=0.0
    )


def _action(**overrides: object) -> dict:
    payload: dict = {
        "kind": ActionKind.CREATE,
        "confidence": 0.8,
        "evidence": "a new memory",
    }
    payload.update(overrides)
    return payload


def test_allowed_action_kinds_are_exactly_the_five_management_actions() -> None:
    assert {kind.value for kind in ActionKind} == {
        "create",
        "link",
        "merge",
        "supersede",
        "conflict",
    }


def test_propose_returns_curator_decision() -> None:
    decision = CuratorDecision(actions=(CuratorAction(**_action()),))
    agent = MemoryCuratorAgent(FakeStructuredLLM([decision]))

    result = agent.propose(_draft(), [])

    assert isinstance(result, CuratorDecision)
    assert result.actions[0].kind is ActionKind.CREATE


def test_unknown_action_is_rejected_by_schema() -> None:
    """未知动作（例如 delete）必须被拒绝。"""
    with pytest.raises(ValidationError):
        CuratorAction(**_action(kind="delete"))


def test_action_cannot_carry_original_evidence_rewrite() -> None:
    """动作不得携带任何改写原始证据的字段。"""
    with pytest.raises(ValidationError):
        CuratorAction(**_action(original_text="rewritten evidence"))


def test_action_evidence_must_not_be_blank() -> None:
    for blank in ("", " ", "\t\n"):
        with pytest.raises(ValidationError):
            CuratorAction(**_action(evidence=blank))


def test_history_is_bounded_by_configuration() -> None:
    history = [_candidate(index) for index in range(20)]
    llm = FakeStructuredLLM([CuratorDecision()])
    agent = MemoryCuratorAgent(llm, max_history=4)

    agent.propose(_draft(), history)

    payload_history = llm.requests[0].payload["history"]
    assert len(payload_history) == 4
    assert payload_history[0]["content"] == "memory 0"


def test_negative_max_history_is_rejected() -> None:
    with pytest.raises(ValueError):
        MemoryCuratorAgent(FakeStructuredLLM(), max_history=-1)


def test_max_history_above_hard_cap_is_clamped() -> None:
    """组件级配置不得突破集中定义的硬上限。"""
    agent = MemoryCuratorAgent(FakeStructuredLLM(), max_history=10_000)

    assert agent.max_history == MAX_HISTORY


def test_zero_max_history_is_legal_and_sends_no_history() -> None:
    history = [_candidate(index) for index in range(5)]
    llm = FakeStructuredLLM([CuratorDecision()])
    agent = MemoryCuratorAgent(llm, max_history=0)

    agent.propose(_draft(), history)

    assert agent.max_history == 0
    assert llm.requests[0].payload["history"] == []


def test_records_model_and_prompt_version() -> None:
    llm = FakeStructuredLLM([CuratorDecision()], model="gpt-4o-mini")
    agent = MemoryCuratorAgent(llm)

    agent.propose(_draft(), [])

    request = llm.requests[0]
    assert request.model == "gpt-4o-mini"
    assert request.prompt_version == "v1"
    assert request.prompt == agent.prompt


def test_agent_rejects_repository_injection() -> None:
    """智能体只接受 Provider 与配置，不接收 Repository 或数据库会话。"""
    llm = FakeStructuredLLM()

    with pytest.raises(TypeError):
        MemoryCuratorAgent(llm, repository=object())
    with pytest.raises(TypeError):
        MemoryCuratorAgent(llm, session=object())


def test_agent_propose_does_not_write_anything() -> None:
    """propose 只返回决策对象，不做任何持久化。"""
    llm = FakeStructuredLLM([CuratorDecision()])
    agent = MemoryCuratorAgent(llm)

    result = agent.propose(_draft(), [])

    assert isinstance(result, CuratorDecision)
    assert not hasattr(agent, "session")
    assert not hasattr(agent, "repository")


def test_prompt_forbids_evidence_rewrites_and_answers() -> None:
    prompt = MemoryCuratorAgent(FakeStructuredLLM()).prompt.lower()

    assert "never rewrite" in prompt or "do not rewrite" in prompt
    assert "original evidence" in prompt
    assert "do not invent memory ids" in prompt


def test_prompt_contains_no_benchmark_examples() -> None:
    prompt = MemoryCuratorAgent(FakeStructuredLLM()).prompt.lower()

    for marker in _FORBIDDEN_PROMPT_MARKERS:
        assert marker not in prompt
