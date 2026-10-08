"""Structured selection returns original evidence or a safe empty/fallback result."""

import json
from uuid import UUID

import pytest

from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import ModelUnavailableError, StructuredOutputError
from masm.retrieval.evidence_selector import DeterministicEvidenceSelector, EvidenceSelector
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ImageURLPart


def _evidence(number: int, source: str, content: str) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id="user-1",
        content=content,
        score=1.0 / number,
        rank=number,
        request_id=source,
        granularity="message",
        source_position=number - 1,
        metadata={"lexical": 0.05},
    )


def test_direct_fact_selection_returns_the_original_evidence_object() -> None:
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": True}], model="gpt-4o-mini"
    )
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")
    irrelevant = _evidence(2, "source-b", "Alice visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", ["Notebook", "Paris"],
        [direct, irrelevant], {direct.memory_id, irrelevant.memory_id},
    )

    assert result.evidence == (direct,)
    assert result.candidate_count == 2
    assert result.source_count == 2
    assert result.selected_source_count == 1
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 1
    assert llm.requests[0].max_attempts == 1
    assert llm.requests[0].prompt_version == "evidence-selector-v1"
    assert llm.requests[0].payload["question"] == "What did Alice buy?"
    assert llm.requests[0].payload["options"] == ["Notebook", "Paris"]


def test_insufficient_decision_abstains_without_fallback() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": False}])
    unrelated = _evidence(1, "source-a", "Alice visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [unrelated], {unrelated.memory_id}
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False


def test_multi_source_question_keeps_both_selected_original_facts() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0, 1], "sufficient_evidence": True}])
    first = _evidence(1, "session-add-1", "Alice purchased a notebook")
    second = _evidence(2, "session-add-2", "Alice gave the notebook to Bob")

    result = EvidenceSelector(llm).select(
        "What did Alice give Bob after buying it?", None,
        [first, second], {first.memory_id, second.memory_id},
    )

    assert result.evidence == (first, second)
    assert result.selected_source_count == 2


def test_selector_payload_is_bounded_text_and_never_contains_image_bytes() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": True}])
    candidate = _evidence(1, "private-request-id", "X" * 1300)
    visual = ImageURLPart(image_url={"url": "data:image/png;base64,cHJpdmF0ZS1ieXRlcw=="})

    EvidenceSelector(llm).select([visual], ["option"], [candidate], {candidate.memory_id})

    payload = llm.requests[0].payload
    assert payload["visual_query_present"] is True
    assert payload["question"] == "[visual query]"
    assert len(payload["candidates"][0]["text"]) == 1200
    assert payload["candidates"][0]["source_group"] == "source-1"
    serialized = json.dumps(payload)
    assert "private-request-id" not in serialized
    assert "cHJpdmF0ZS1ieXRlcw==" not in serialized


def test_empty_pool_abstains_without_model_request() -> None:
    llm = FakeStructuredLLM()

    result = EvidenceSelector(llm).select("unknown question", None, [], set())

    assert result.evidence == ()
    assert result.abstained is True
    assert llm.requests == []


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (ModelUnavailableError("private response"), "unavailable"),
        (TimeoutError("private response"), "unavailable"),
        (StructuredOutputError("private response"), "invalid_output"),
        ({"selected_indices": [True], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": ["bad"], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [-1], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [2], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [0, 0], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [], "sufficient_evidence": True}, "invalid_output"),
    ],
)
def test_invalid_selection_falls_back_to_question_admitted_anchor(failure, category) -> None:
    llm = FakeStructuredLLM([failure])
    anchor = _evidence(1, "source-a", "the needed fact")
    relation_only = _evidence(2, "source-b", "relation expansion without recall signal")

    result = EvidenceSelector(llm).select(
        "needed fact", None, [anchor, relation_only], {anchor.memory_id}
    )

    assert result.evidence == (anchor,)
    assert result.fallback is True
    assert result.abstained is False
    assert result.failure_category == category
    assert len(llm.requests) == 1


def test_failed_selection_with_no_strong_anchor_abstains() -> None:
    candidate = _evidence(1, "source-a", "unrelated fact")
    selector = EvidenceSelector(FakeStructuredLLM([TimeoutError("private body")]))

    result = selector.select("missing fact", None, [candidate], set())

    assert result.evidence == ()
    assert result.fallback is True
    assert result.abstained is True


def test_over_limit_selection_falls_back_to_at_most_twelve_anchors() -> None:
    ranked = [_evidence(number, f"source-{number}", f"fact {number}") for number in range(1, 15)]
    llm = FakeStructuredLLM(
        [{"selected_indices": list(range(13)), "sufficient_evidence": True}]
    )

    result = EvidenceSelector(llm).select(
        "facts", None, ranked, {item.memory_id for item in ranked}
    )

    assert result.fallback is True
    assert len(result.evidence) == 12


def test_fallback_diversifies_sources_and_preserves_rank_order() -> None:
    ranked = [
        _evidence(1, "frequent-source", "fact 1"),
        _evidence(2, "frequent-source", "fact 2"),
        _evidence(3, "frequent-source", "fact 3"),
        _evidence(4, "frequent-source", "fact 4"),
        _evidence(5, "rare-source", "fact 5"),
    ]
    selector = EvidenceSelector(
        FakeStructuredLLM([TimeoutError()]), max_candidates=5, max_selected=4
    )

    result = selector.select("facts", None, ranked, {item.memory_id for item in ranked})

    assert [item.memory_id for item in result.evidence] == [
        UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=5)
    ]


def test_selector_limits_question_and_option_text_in_provider_payload() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("Q" * 5000, ["O" * 800] * 30, [candidate], set())

    payload = llm.requests[0].payload
    assert len(payload["question"]) == 4000
    assert len(payload["options"]) == 16
    assert all(len(option) == 512 for option in payload["options"])


def test_deterministic_selector_passes_through_existing_local_fake_results() -> None:
    ranked = tuple(_evidence(number, "source-a", f"fact {number}") for number in range(1, 101))

    result = DeterministicEvidenceSelector().select("facts", None, ranked, set())

    assert result.evidence == ranked
    assert result.fallback is False
    assert result.abstained is False
