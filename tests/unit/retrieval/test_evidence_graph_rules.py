"""Deterministic grounding rules: normalization, discriminative units and structural checks."""

from uuid import UUID

import pytest

from masm.retrieval.evidence_graph import (
    MAX_GRAPH_LINKS,
    MAX_GRAPH_REQUIREMENTS,
    MAX_GRAPH_SUPPORTS,
    EvidenceGraphProposal,
    GraphContext,
)
from masm.retrieval.evidence_graph_rules import (
    CandidateView,
    ValidationOutcome,
    absent_name_tokens,
    candidate_views,
    discriminative_units,
    grounded_quote,
    normalize_text,
    planner_candidate_text,
    shared_discriminative_unit,
    validate_proposal,
)
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import TextPart

_USER = "user-1"


def _evidence(
    number: int,
    source: str,
    content: str | list[object],
    *,
    position: int = 0,
    granularity: str = "message",
    source_context: str | None = None,
    user_id: str = _USER,
) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id=user_id,
        content=content,  # type: ignore[arg-type]
        score=1.0 / number,
        rank=number,
        request_id=source,
        granularity=granularity,  # type: ignore[arg-type]
        source_position=position,
        source_context=source_context,
        metadata={"lexical": 0.05},
    )


def _views(*items: RankedEvidence, max_chars: int = 1200) -> tuple[CandidateView, ...]:
    return candidate_views(list(items), max_chars=max_chars)


def _proposal(**overrides: object) -> EvidenceGraphProposal:
    payload: dict[str, object] = {"requirements": [], "supports": [], "links": []}
    payload.update(overrides)
    return EvidenceGraphProposal.model_validate(payload)


def _requirement(quote: str, kind: str = "attribute") -> dict[str, object]:
    return {"question_quote": quote, "kind": kind}


def _support(
    requirement_index: int, candidate_index: int, quote: str, role: str = "direct"
) -> dict[str, object]:
    return {
        "requirement_index": requirement_index,
        "candidate_index": candidate_index,
        "candidate_quote": quote,
        "role": role,
    }


def _link(left: int, right: int, left_quote: str, right_quote: str, kind: str) -> dict[str, object]:
    return {
        "left_candidate_index": left,
        "right_candidate_index": right,
        "left_quote": left_quote,
        "right_quote": right_quote,
        "kind": kind,
    }


def _validate(
    proposal: EvidenceGraphProposal,
    *,
    question: str,
    views: tuple[CandidateView, ...],
    context: GraphContext | None = None,
    max_requirements: int = MAX_GRAPH_REQUIREMENTS,
    max_supports: int = MAX_GRAPH_SUPPORTS,
    max_links: int = MAX_GRAPH_LINKS,
) -> ValidationOutcome:
    return validate_proposal(
        proposal,
        question=question,
        candidates=views,
        context=context if context is not None else GraphContext(),
        expected_user_id=_USER,
        max_requirements=max_requirements,
        max_supports=max_supports,
        max_links=max_links,
    )


# --- normalization and grounding ---------------------------------------------------------


def test_requirement_quotes_tolerate_punctuation_only_differences() -> None:
    views = _views(_evidence(1, "source-a", "The check-in code is K7-42."))
    outcome = _validate(
        _proposal(
            requirements=[
                _requirement("What is the check-in code, and where is the venue")
            ]
        ),
        question="What is the check-in code and where is the venue?",
        views=views,
    )

    # 逗号/问号等标点差异不改变“引文就是问题原文”，不得判为结构失败。
    assert outcome.valid is True
    assert outcome.reason == "none"


def test_reworded_requirement_quotes_report_an_actionable_reason() -> None:
    views = _views(_evidence(1, "source-a", "The check-in code is K7-42."))
    question = "What is the check-in code and where is the venue?"
    reordered = _validate(
        _proposal(
            requirements=[
                _requirement("Where is the venue and what is the check-in code")
            ]
        ),
        question=question,
        views=views,
    )
    paraphrased = _validate(
        _proposal(
            requirements=[
                _requirement("What is the check-in passcode and where is the venue")
            ]
        ),
        question=question,
        views=views,
    )

    assert reordered.valid is False
    assert reordered.reason == "requirement_quote_reordered"
    assert paraphrased.valid is False
    assert paraphrased.reason == "requirement_quote_paraphrased"


def test_normalization_folds_case_width_and_whitespace_without_deleting_negation() -> None:
    assert normalize_text("I have NOT  stored\u00a0it") == "i have not stored it"
    assert normalize_text("ＤＲＡＷＥＲ　ｔｈｒｅｅ") == "drawer three"
    assert normalize_text("  我把琥珀钥匙\n放在抽屉里  ") == "我把琥珀钥匙 放在抽屉里"
    assert "not" in normalize_text("I have not stored it")
    assert "不" in normalize_text("我没有说 不")


def test_grounded_quote_requires_a_normalized_exact_substring() -> None:
    text = "I have not stored the amber key in drawer three."
    assert grounded_quote("I have not stored the amber key", text) is True
    assert grounded_quote("i   HAVE not\u00a0stored", text) is True
    assert grounded_quote("I have stored the amber key", text) is False
    assert grounded_quote("drawer four", text) is False
    assert grounded_quote("", text) is False


# --- discriminative units ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("The Zephyr code was chosen.", "The review of Zephyr is next.", "zephyr"),
        ("The Lisbon venue is booked.", "We meet in Lisbon.", "lisbon"),
        ("The amber key is here.", "Where the amber key goes.", "amber key"),
        ("We discussed 2026 planning.", "The 2026 planning notes.", "2026"),
        ("我报名了陶艺工作坊。", "陶艺工作坊在里斯本举办。", "陶艺"),
    ],
)
def test_discriminative_units_accept_explicit_shared_evidence(
    left: str, right: str, expected: str
) -> None:
    assert shared_discriminative_unit(left, right, "irrelevant question") == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Nora attended the workshop.", "Nora liked the workshop."),
        ("We moved to Porto.", "Porto is nice."),
        ("The project workshop was planned.", "The workshop takes place every spring."),
        ("The meeting was long.", "Another meeting is scheduled."),
        ("The city is crowded.", "A city in the north."),
        ("I counted 42 items.", "There were 42 items."),
        ("...", "!!!"),
        ("我报名了一个工作坊。", "这个工作坊在里斯本举办。"),
        ("这个项目很好。", "那个项目也不错。"),
    ],
)
def test_discriminative_units_reject_generic_short_or_number_only_units(
    left: str, right: str
) -> None:
    assert shared_discriminative_unit(left, right, "unrelated question") is None


def test_units_present_in_the_question_cannot_connect_candidates() -> None:
    left = "The Zephyr code was chosen."
    right = "The review of Zephyr is in April."
    assert shared_discriminative_unit(left, right, "What is the code name?") == "zephyr"
    assert shared_discriminative_unit(left, right, "What about Zephyr?") is None


def test_discriminative_units_are_deterministic_and_skip_generic_nouns() -> None:
    units = discriminative_units("The Zephyr workshop is held in Lisbon.")
    assert units == discriminative_units("The Zephyr workshop is held in Lisbon.")
    assert "zephyr" in units
    assert "lisbon" in units
    assert "workshop" not in units
    assert discriminative_units("   ") == frozenset()


# --- planner-visible text ----------------------------------------------------------------


def test_planner_candidate_text_prefers_source_context_and_truncates() -> None:
    item = _evidence(1, "source-a", "Yes.", source_context='{"matched_position":1}')
    assert planner_candidate_text(item, 1200) == '{"matched_position":1}'
    assert planner_candidate_text(item, 5) == '{"mat'
    plain = _evidence(2, "source-a", "A plain summary.")
    assert planner_candidate_text(plain, 1200) == "A plain summary."
    multimodal = _evidence(
        3, "source-a", [TextPart(text="first part"), TextPart(text="second part")]
    )
    assert planner_candidate_text(multimodal, 1200) == "first part second part"


def test_candidate_views_label_sources_by_first_appearance() -> None:
    first = _evidence(1, "source-a", "One.")
    second = _evidence(2, "source-b", "Two.")
    third = _evidence(3, "source-a", "Three.")

    views = _views(first, second, third)

    assert [view.index for view in views] == [0, 1, 2]
    assert [view.source_group for view in views] == ["source-1", "source-2", "source-1"]
    assert [view.request_id for view in views] == ["source-a", "source-b", "source-a"]
    assert views[0].text == "One."
    assert views[0].evidence is first


# --- structural validation ---------------------------------------------------------------


def test_validator_rejects_foreign_candidate_views_for_the_request_user() -> None:
    mine = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    foreign = _evidence(
        2,
        "source-b",
        "Foreign memory text.",
        user_id="other-user",
    )
    proposal = _proposal(
        requirements=[_requirement("Where did I store the amber key", "location")],
        supports=[_support(0, 0, "I stored the amber key in drawer three.", "direct")],
    )

    outcome = validate_proposal(
        proposal,
        question="Where did I store the amber key?",
        candidates=_views(mine, foreign),
        context=GraphContext(),
        expected_user_id=_USER,
    )

    assert outcome.valid is False
    assert outcome.failure_category == "planner_invalid"


def test_too_many_requirements_supports_or_links_invalidate_the_whole_proposal() -> None:
    views = _views(_evidence(1, "source-a", "One."), _evidence(2, "source-b", "Two."))
    question = "Where is the amber key?"
    grounded = _requirement(question, "location")

    too_many_requirements = _proposal(requirements=[grounded] * (MAX_GRAPH_REQUIREMENTS + 1))
    assert _validate(too_many_requirements, question=question, views=views).valid is False

    too_many_supports = _proposal(
        requirements=[grounded],
        supports=[_support(0, 0, "One.", "direct")] * (MAX_GRAPH_SUPPORTS + 1),
    )
    assert _validate(too_many_supports, question=question, views=views).valid is False

    too_many_links = _proposal(
        requirements=[grounded],
        supports=[_support(0, 0, "One.", "direct")],
        links=[_link(0, 1, "One.", "Two.", "shared_explicit_mention")] * (MAX_GRAPH_LINKS + 1),
    )
    assert _validate(too_many_links, question=question, views=views).valid is False


def test_caps_can_be_lowered_by_configuration() -> None:
    views = _views(_evidence(1, "source-a", "One."))
    question = "Where is the amber key?"
    proposal = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(0, 0, "One.", "direct")],
    )
    allowed = _validate(proposal, question=question, views=views)
    assert allowed.valid is True
    assert len(allowed.supports) == 1
    assert _validate(proposal, question=question, views=views, max_supports=0).valid is False


def test_unmatched_requirement_quotes_invalidate_the_whole_proposal() -> None:
    views = _views(_evidence(1, "source-a", "One."))
    proposal = _proposal(
        requirements=[_requirement("Where is the amber key", "location")],
        supports=[_support(0, 0, "One.", "direct")],
    )
    outcome = _validate(proposal, question="What is the storage code?", views=views)
    assert outcome.valid is False
    assert outcome.failure_category == "planner_invalid"


def test_out_of_range_indices_invalidate_the_proposal() -> None:
    views = _views(_evidence(1, "source-a", "One."), _evidence(2, "source-b", "Two."))
    question = "Where is the amber key?"

    out_of_range_requirement = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(4, 0, "One.", "direct")],
    )
    assert _validate(out_of_range_requirement, question=question, views=views).valid is False

    out_of_range_candidate = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(0, 7, "One.", "direct")],
    )
    assert _validate(out_of_range_candidate, question=question, views=views).valid is False

    self_link = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(0, 0, "One.", "direct")],
        links=[_link(1, 1, "Two.", "Two.", "same_source_context")],
    )
    assert _validate(self_link, question=question, views=views).valid is False

    out_of_range_link = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(0, 0, "One.", "direct")],
        links=[_link(0, 9, "One.", "Two.", "shared_explicit_mention")],
    )
    assert _validate(out_of_range_link, question=question, views=views).valid is False


def test_duplicate_items_are_deduplicated() -> None:
    views = _views(_evidence(1, "source-a", "One."), _evidence(2, "source-b", "Two."))
    question = "Where is the amber key?"
    proposal = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[_support(0, 0, "One.", "direct")] * 2,
        links=[
            _link(0, 1, "One.", "Two.", "shared_explicit_mention"),
            _link(0, 1, "One.", "Two.", "shared_explicit_mention"),
        ],
    )
    outcome = _validate(proposal, question=question, views=views)
    assert outcome.valid is True
    assert len(outcome.supports) == 1


def test_ungrounded_item_quotes_are_dropped_without_invalidating_the_proposal() -> None:
    views = _views(
        _evidence(1, "source-a", "I stored the amber key in drawer three."),
        _evidence(2, "source-b", "The shed holds bicycles."),
    )
    question = "Where did I store the amber key?"
    proposal = _proposal(
        requirements=[_requirement(question, "location")],
        supports=[
            _support(0, 0, "I stored the amber key in drawer three.", "direct"),
            _support(0, 1, "I stored the amber key in drawer nine.", "direct"),
            _support(0, 1, "The shed holds bicycles.", "context"),
        ],
        links=[
            _link(
                0,
                1,
                "I stored the amber key in drawer three.",
                "missing text.",
                "shared_explicit_mention",
            )
        ],
    )

    outcome = _validate(proposal, question=question, views=views)

    assert outcome.valid is True
    assert [support.candidate_index for support in outcome.supports] == [0, 1]
    assert outcome.links == ()


def test_edge_kinds_are_validated_against_real_provenance() -> None:
    adjacent_first = _evidence(1, "source-a", "Did you move to Porto?", position=0)
    adjacent_second = _evidence(2, "source-a", "Yes, last week.", position=1)
    distant = _evidence(3, "source-a", "Unrelated later note.", position=9)
    other_source = _evidence(4, "source-b", "Yes, last week.", position=0)
    views = _views(adjacent_first, adjacent_second, distant, other_source)
    question = "Which city did I move to?"
    requirements = [_requirement(question, "location")]

    adjacent = _proposal(
        requirements=requirements,
        supports=[_support(0, 1, "Yes, last week.", "direct")],
        links=[_link(0, 1, "Did you move to Porto?", "Yes, last week.", "confirmation")],
    )
    with_context = _validate(
        adjacent,
        question=question,
        views=views,
        context=GraphContext(source_context_enabled=True),
    )
    assert len(with_context.links) == 1
    without_context = _validate(
        adjacent,
        question=question,
        views=views,
        context=GraphContext(source_context_enabled=False),
    )
    assert without_context.links == ()

    non_adjacent = _proposal(
        requirements=requirements,
        supports=[_support(0, 1, "Yes, last week.", "direct")],
        links=[_link(0, 2, "Did you move to Porto?", "Unrelated later note.", "confirmation")],
    )
    assert _validate(non_adjacent, question=question, views=views).links == ()

    cross_source = _proposal(
        requirements=requirements,
        supports=[_support(0, 1, "Yes, last week.", "direct")],
        links=[_link(0, 3, "Did you move to Porto?", "Yes, last week.", "confirmation")],
    )
    assert _validate(cross_source, question=question, views=views).links == ()

    persisted = _proposal(
        requirements=requirements,
        supports=[_support(0, 1, "Yes, last week.", "direct")],
        links=[_link(0, 3, "Did you move to Porto?", "Yes, last week.", "persisted_relation")],
    )
    assert _validate(persisted, question=question, views=views).links == ()
    with_sidecar = _validate(
        persisted,
        question=question,
        views=views,
        context=GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
    )
    assert len(with_sidecar.links) == 1


def test_shared_mention_requires_both_quotes_to_be_grounded() -> None:
    first = _evidence(1, "source-a", "The Zephyr code was chosen.")
    second = _evidence(2, "source-b", "The review of Zephyr is in April.")
    views = _views(first, second)
    question = "What is the code name?"
    proposal = _proposal(
        requirements=[_requirement(question, "attribute")],
        supports=[
            _support(0, 0, "The Zephyr code was chosen.", "direct"),
            _support(0, 1, "The review of Zephyr is in April.", "direct"),
        ],
        links=[
            _link(
                0,
                1,
                "The Zephyr code was chosen.",
                "Zephyr appears only in this fabricated quote.",
                "shared_explicit_mention",
            )
        ],
    )
    assert _validate(proposal, question=question, views=views).links == ()

def test_absent_name_tokens_flags_a_subject_missing_from_the_pool() -> None:
    pool = (
        "the access code for item-42 locker is code-7",
        "my sister maya works at the office-9",
    )

    assert absent_name_tokens(
        "What is the access code for the lockB-77 locker?", pool
    ) == frozenset({"lockb-77"})


def test_absent_name_tokens_ignores_present_names_and_plain_words() -> None:
    pool = (
        "the access code for item-42 locker is code-7",
        "my sister maya works at the office-9",
    )

    # 池里存在的名字、以及没有任何"名字样"特征的问题都不触发。
    assert absent_name_tokens("What is the code for item-42?", pool) == frozenset()
    assert absent_name_tokens("What is the access code for the locker?", pool) == frozenset()
    assert absent_name_tokens("Which city hosts the workshop?", pool) == frozenset()

def test_plain_capitalised_names_are_not_subject_identifiers() -> None:
    pool = ("The Lisbon-office contact is Ana.",)

    # 普通首字母大写（Porto、Alice）不算标识符：官方 direct 问题普遍如此，据此拒答会清零 direct。
    assert absent_name_tokens(
        "Which office is the Porto-office contact for?", pool
    ) == frozenset()
    assert absent_name_tokens("What did Alice say about her sister?", pool) == frozenset()
    # 标识符（含数字或词内大写）仍然参与判定。
    assert absent_name_tokens(
        "What is the code for the AbsentSubject-zz99yy88xx locker?", pool
    ) == frozenset({"absentsubject-zz99yy88xx"})
    # 日历与代词不是主体名，不得据此拒答。
    assert absent_name_tokens("What did I file on Monday?", pool) == frozenset()

def test_absent_name_tokens_uses_numeric_parts_for_short_names() -> None:
    pool = ("The access code for the item-42 locker is code-7.",)

    # 区分部分是短数字时也要判定：item-47 未存、item-42 已存。
    assert absent_name_tokens(
        "What is the code for the item-47 locker?", pool
    ) == frozenset({"item-47"})
    assert absent_name_tokens("What is the code for the item-42 locker?", pool) == frozenset()
    # 整词仍要求 4 字符：极短数字不会被当成名字。
    assert absent_name_tokens("List my 3 most recent notes.", pool) == frozenset()
