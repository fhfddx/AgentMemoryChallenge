"""Structured selection returns original evidence or a safe empty/fallback result."""

import json
from typing import Literal
from uuid import UUID

import pytest

from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import ModelUnavailableError, StructuredOutputError
from masm.retrieval.evidence_selector import (
    DeterministicEvidenceSelector,
    EvidenceSelection,
    EvidenceSelector,
)
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ImageURLPart, TextPart


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
    assert llm.requests[0].prompt_version == "evidence-selector-v5"
    assert llm.requests[0].payload["question"] == "What did Alice buy?"
    assert llm.requests[0].payload["options"] == ["Notebook", "Paris"]


def test_insufficient_decision_without_indices_abstains_without_fallback() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    purchase = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?",
        None,
        [purchase, unrelated],
        set(),
    )

    assert result.evidence == ()
    assert result.fallback is False
    assert result.abstained is True
    assert result.evidence_state == "insufficient"


def test_inconsistent_insufficient_decision_normalizes_to_safe_abstention() -> None:
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": False}]
    )
    anchor = _evidence(1, "source-a", "Alice bought a blue notebook")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [anchor], {anchor.memory_id}
    )

    assert result.evidence == ()
    assert result.fallback is False
    assert result.abstained is True
    assert result.failure_category == "invalid_output"
    assert result.evidence_state == "insufficient"


def test_partial_direct_evidence_is_retained_instead_of_being_discarded() -> None:
    """成对用例 A：模型声明 partial 并给出直接事实，不得丢弃全部召回。

    `partial` 表示「选中的记忆直接陈述了某个被询问的事实，但不足以覆盖全部询问」。
    这是二态协议无法表达的边界：既不能无条件拒答，也不能回落到旧式全量强锚点。
    """
    llm = FakeStructuredLLM([{"selected_indices": [0], "evidence_state": "partial"}])
    purchase = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?",
        None,
        [purchase, unrelated],
        {purchase.memory_id, unrelated.memory_id},
    )

    assert result.evidence == (purchase,)
    assert result.abstained is False
    assert result.fallback is False
    assert result.evidence_state == "partial"
    assert result.failure_category == "none"


def test_missing_link_evidence_still_abstains_without_unconditional_fallback() -> None:
    """成对用例 B：候选只是局部推理链，必须拒答，不得恢复旧式无条件回退。

    模型明确声明 `insufficient` 时，即使 indices 非空也必须拒答；旧行为会把这类
    矛盾输出回落到全量 question-admitted 强锚点，从而在证据不足时仍然作答。
    """
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "evidence_state": "insufficient"}]
    )
    local_link = _evidence(1, "source-a", "Nora plans to attend the Zephyr workshop")
    receipt = _evidence(2, "source-b", "Nora filed a travel receipt on Tuesday")
    unrelated = _evidence(3, "source-c", "The training room has a blue clock")

    result = EvidenceSelector(llm).select(
        "In which city is the workshop that Nora plans to attend?",
        None,
        [local_link, receipt, unrelated],
        {item.memory_id for item in (local_link, receipt, unrelated)},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def test_partial_without_usable_indices_abstains_instead_of_falling_back() -> None:
    """partial 但没有可用索引时没有可引用事实，按拒答处理而不是强锚点回退。"""
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "partial"}])
    anchor = _evidence(1, "source-a", "the needed fact")

    result = EvidenceSelector(llm).select(
        "the needed fact", None, [anchor], {anchor.memory_id}
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def test_legacy_two_field_output_is_accepted_and_kept_on_the_safe_side() -> None:
    """旧 Provider 只有布尔位，无法表达 partial，因此矛盾输出保持拒答。"""
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": False}])
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?", None,
        [direct, unrelated], {direct.memory_id, unrelated.memory_id},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def test_legacy_two_field_sufficient_output_still_selects_evidence() -> None:
    """旧 Provider 的 sufficient_evidence=true 仍映射为 sufficient，行为不变。"""
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": True}])
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [direct], {direct.memory_id}
    )

    assert result.evidence == (direct,)
    assert result.abstained is False
    assert result.fallback is False
    assert result.evidence_state == "sufficient"


def test_selector_requests_deterministic_three_state_decision() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    entity_only = _evidence(1, "source-a", "Alice stored a notebook in cabinet seven")

    EvidenceSelector(llm).select(
        "What is the notebook serial number?", None,
        [entity_only], {entity_only.memory_id},
    )

    request = llm.requests[0]
    assert request.temperature == 0.0
    assert request.prompt_version == "evidence-selector-v5"
    assert "smallest set of memories" in request.prompt
    assert "every attribute, relation, value, or event asked for" in request.prompt
    assert "distinct source_group" in request.prompt
    assert "evidence_state=partial" in request.prompt
    assert "evidence_state=insufficient" in request.prompt
    assert "local fragment of a longer reasoning chain" in request.prompt


def test_selection_schema_accepts_only_the_two_protocol_fields() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("fact", None, [candidate], {candidate.memory_id})

    assert EvidenceSelection.model_config["extra"] == "forbid"
    assert set(EvidenceSelection.model_fields) == {"selected_indices", "evidence_state"}
    assert EvidenceSelection.model_fields["evidence_state"].annotation == Literal[
        "sufficient", "partial", "insufficient"
    ]


def test_no_direct_evidence_decision_abstains_without_fallback() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
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


def test_incomplete_first_pass_retries_with_deeper_candidates() -> None:
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 47], "sufficient_evidence": True},
        ]
    )

    result = EvidenceSelector(llm).select(
        "Which two facts complete the answer?", None, ranked, set()
    )

    assert result.evidence == (ranked[0], ranked[47])
    assert result.candidate_count == 48
    assert result.selected_source_count == 2
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 2
    assert len(llm.requests[0].payload["candidates"]) == 32
    assert len(llm.requests[1].payload["candidates"]) == 48


def test_sufficient_first_pass_does_not_retry_deeper_candidates() -> None:
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": True}]
    )

    result = EvidenceSelector(llm).select(
        "Which fact completes the answer?", None, ranked, set()
    )

    assert result.evidence == (ranked[0],)
    assert result.candidate_count == 32
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 1


def test_atomic_fact_survives_even_when_its_rank_exceeds_response_cap() -> None:
    ranked = [_evidence(number, f"source-{number}", f"fact {number}") for number in range(1, 15)]
    llm = FakeStructuredLLM([{"selected_indices": [12], "sufficient_evidence": True}])

    result = EvidenceSelector(llm, max_candidates=14).select(
        "Which exact fact is needed?", None, ranked, {item.memory_id for item in ranked}
    )

    assert result.evidence == (ranked[12],)
    assert result.fallback is False
    assert result.candidate_count == 14


@pytest.mark.parametrize(
    ("question", "options", "content"),
    [
        ("What color was Alice's notebook?", ["blue", "green"], "Bob drove a blue car"),
        ("What did Alice buy?", None, "Alice visited Paris"),
    ],
)
def test_option_only_or_entity_only_memory_abstains(
    question: str, options: list[str] | None, content: str
) -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    candidate = _evidence(1, "source-a", content)

    result = EvidenceSelector(llm).select(question, options, [candidate], {candidate.memory_id})

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert llm.requests[0].payload["question"] == question


def test_candidate_text_limit_cannot_be_configured_above_hard_cap() -> None:
    with pytest.raises(ValueError, match="max_chars_per_candidate"):
        EvidenceSelector(FakeStructuredLLM(), max_chars_per_candidate=4097)


def test_selector_payload_is_bounded_text_and_never_contains_image_bytes() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": True}])
    candidate = _evidence(1, "private-request-id", "X" * 1300)
    visual = ImageURLPart(image_url={"url": "data:image/png;base64,cHJpdmF0ZS1ieXRlcw=="})

    EvidenceSelector(llm).select(
        [TextPart(text="What is shown?"), visual], ["option"],
        [candidate], {candidate.memory_id},
    )

    payload = llm.requests[0].payload
    assert payload["visual_query_present"] is True
    assert payload["question"] == "What is shown?"
    assert len(payload["candidates"][0]["text"]) == 1200
    assert payload["candidates"][0]["source_group"] == "source-1"
    serialized = json.dumps(payload)
    assert "private-request-id" not in serialized
    assert "cHJpdmF0ZS1ieXRlcw==" not in serialized


def test_pure_visual_query_preserves_image_recall_without_uninformed_model_call() -> None:
    visual = ImageURLPart(image_url={"url": "data:image/png;base64,cHJpdmF0ZS1ieXRlcw=="})
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    anchor = _evidence(1, "image-request", "a blue bicycle beside a tree")
    relation_only = _evidence(2, "related-request", "unverified related context")

    result = EvidenceSelector(llm).select(
        [visual], None, [anchor, relation_only], {anchor.memory_id}
    )

    assert result.evidence == (anchor,)
    assert result.fallback is True
    assert result.failure_category == "none"
    assert result.abstained is False
    assert llm.requests == []


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
