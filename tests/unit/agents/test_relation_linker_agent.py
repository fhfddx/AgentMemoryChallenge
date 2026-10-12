"""Dedicated Add-time relation linker tests."""

from uuid import uuid4

from masm.agents import MAX_HISTORY
from masm.agents.relation_linker import RelationLinkerAgent
from masm.providers.fakes import FakeStructuredLLM
from masm.schemas.agents import RelationLinkDecision
from masm.storage.types import MemoryCandidate, MemoryDraft

_FORBIDDEN_PROMPT_MARKERS = (
    "answer:",
    "correct answer",
    "example question",
    "expected output",
    "benchmark",
    "q:",
)


def _candidate(index: int) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=uuid4(),
        user_id="user-1",
        content=f"historical evidence {index}",
        score=float(MAX_HISTORY - index),
    )


def _draft() -> MemoryDraft:
    return MemoryDraft(
        summary="derived summary",
        original_text="the current immutable evidence",
        keywords=("current",),
    )


def test_relation_linker_sends_original_text_and_bounded_candidate_ids() -> None:
    history = [_candidate(index) for index in range(MAX_HISTORY + 4)]
    llm = FakeStructuredLLM([RelationLinkDecision()])
    agent = RelationLinkerAgent(llm)

    agent.propose(_draft(), history)

    payload = llm.requests[0].payload
    assert set(payload) == {"current_memory", "history"}
    assert payload["current_memory"] == {
        "original_text": "the current immutable evidence"
    }
    assert len(payload["history"]) == MAX_HISTORY
    assert [row["memory_id"] for row in payload["history"]] == [
        str(candidate.memory_id) for candidate in history[:MAX_HISTORY]
    ]
    assert [row["content"] for row in payload["history"]] == [
        candidate.content for candidate in history[:MAX_HISTORY]
    ]


def test_relation_linker_records_model_and_prompt_version() -> None:
    llm = FakeStructuredLLM([RelationLinkDecision()], model="gpt-4o-mini")
    agent = RelationLinkerAgent(llm)

    result = agent.propose(_draft(), [])

    assert result == RelationLinkDecision()
    request = llm.requests[0]
    assert request.model == "gpt-4o-mini"
    assert request.prompt_version == "relation-linker-v1"
    assert request.prompt == agent.prompt


def test_relation_linker_uses_deterministic_sampling_for_repeatable_links() -> None:
    llm = FakeStructuredLLM([RelationLinkDecision()])
    agent = RelationLinkerAgent(llm)

    agent.propose(_draft(), [_candidate(0)])

    assert llm.requests[0].temperature == 0.0


def test_relation_linker_prompt_requires_factual_connection_and_forbids_topic_only_links() -> None:
    prompt = " ".join(RelationLinkerAgent(FakeStructuredLLM()).prompt.split()).casefold()

    assert "factual continuation" in prompt
    assert "same concrete event" in prompt
    assert "topic" in prompt
    assert "not a link" in prompt
    assert "exact quote" in prompt


def test_relation_linker_prompt_contains_no_benchmark_examples() -> None:
    prompt = RelationLinkerAgent(FakeStructuredLLM()).prompt.casefold()

    for marker in _FORBIDDEN_PROMPT_MARKERS:
        assert marker not in prompt


def test_relation_linker_max_history_is_bounded() -> None:
    assert RelationLinkerAgent(FakeStructuredLLM(), max_history=10_000).max_history == MAX_HISTORY
