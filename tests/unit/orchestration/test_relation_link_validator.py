"""Deterministic validation for Add-time relation-link proposals."""

from uuid import UUID, uuid4

from masm.orchestration.relation_link_validator import (
    MAX_LINKS,
    MIN_LINK_CONFIDENCE,
    merge_validated_actions,
    validate_relation_links,
)
from masm.schemas.agents import RelationLinkDecision, RelationLinkProposal
from masm.storage.types import MemoryCandidate, RelationDraft, ValidatedActions


def _candidate(
    *,
    memory_id: UUID | None = None,
    user_id: str = "user-1",
    content: str = "The Atlas initiative begins next spring.",
    granularity: str = "context",
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=memory_id or uuid4(),
        user_id=user_id,
        content=content,
        score=1.0,
        granularity=granularity,  # type: ignore[arg-type]
    )


def _proposal(
    target: UUID,
    *,
    current_evidence: str = "The internal codename is Zephyr.",
    target_evidence: str = "The Atlas initiative begins next spring.",
    confidence: float = MIN_LINK_CONFIDENCE,
) -> RelationLinkProposal:
    return RelationLinkProposal(
        target_memory_id=target,
        current_evidence=current_evidence,
        target_evidence=target_evidence,
        confidence=confidence,
    )


def _validate(
    decision: RelationLinkDecision,
    candidates: list[MemoryCandidate],
    **kwargs: object,
) -> tuple[RelationDraft, ...]:
    return validate_relation_links(
        "user-1",
        decision,
        "The internal codename is Zephyr.",
        candidates,
        **kwargs,  # type: ignore[arg-type]
    )


def test_accepts_grounded_same_user_context_link_at_frozen_threshold() -> None:
    candidate = _candidate()

    links = _validate(
        RelationLinkDecision(links=(_proposal(candidate.memory_id),)),
        [candidate],
    )

    assert MIN_LINK_CONFIDENCE == 0.80
    assert MAX_LINKS == 4
    assert [(link.target_id, link.relation_type) for link in links] == [
        (candidate.memory_id, "link")
    ]


def test_accepts_quotes_after_whitespace_normalization_only() -> None:
    candidate = _candidate(content="The Atlas\n initiative   begins next spring.")
    proposal = _proposal(
        candidate.memory_id,
        current_evidence="The internal   codename is Zephyr.",
        target_evidence="The Atlas initiative begins next spring.",
    )

    links = validate_relation_links(
        "user-1",
        RelationLinkDecision(links=(proposal,)),
        "The internal\n codename is Zephyr.",
        [candidate],
    )

    assert len(links) == 1


def test_rejects_case_or_character_changed_quote() -> None:
    candidate = _candidate()
    wrong_case = _proposal(candidate.memory_id, current_evidence="the internal codename is Zephyr.")
    wrong_character = _proposal(
        candidate.memory_id,
        target_evidence="The Atlas plan begins next spring.",
    )

    links = _validate(RelationLinkDecision(links=(wrong_case, wrong_character)), [candidate])

    assert links == ()


def test_ignores_foreign_unknown_and_message_targets() -> None:
    foreign = _candidate(user_id="user-2")
    message = _candidate(granularity="message")
    unknown = uuid4()
    decision = RelationLinkDecision(
        links=(
            _proposal(foreign.memory_id),
            _proposal(message.memory_id),
            _proposal(unknown),
        )
    )

    assert _validate(decision, [foreign, message]) == ()


def test_ignores_low_confidence_duplicate_and_over_cap_proposals() -> None:
    candidates = [_candidate() for _ in range(3)]
    decision = RelationLinkDecision(
        links=(
            _proposal(candidates[0].memory_id, confidence=0.79),
            _proposal(candidates[1].memory_id),
            _proposal(candidates[1].memory_id),
            _proposal(candidates[2].memory_id),
        )
    )

    links = _validate(decision, candidates, max_links=1)

    assert [link.target_id for link in links] == [candidates[1].memory_id]


def test_relation_evidence_ref_contains_both_grounded_quotes() -> None:
    candidate = _candidate()
    proposal = _proposal(candidate.memory_id)

    link = _validate(RelationLinkDecision(links=(proposal,)), [candidate])[0]

    assert link.evidence_ref == {
        "kind": "link",
        "current_evidence": proposal.current_evidence,
        "target_evidence": proposal.target_evidence,
    }
    assert link.confidence == MIN_LINK_CONFIDENCE


def test_curator_relation_wins_when_linker_targets_the_same_memory() -> None:
    target = uuid4()
    curator = RelationDraft(target_id=target, relation_type="conflict", confidence=0.95)
    linker = RelationDraft(target_id=target, relation_type="link", confidence=0.9)

    merged = merge_validated_actions(ValidatedActions(relations=(curator,)), (linker,))

    assert merged.relations == (curator,)


def test_merge_preserves_duplicate_supersede_conflict_fields() -> None:
    duplicate = uuid4()
    supersedes = uuid4()
    conflict_group = uuid4()
    conflict_target = uuid4()
    existing = ValidatedActions(
        duplicate_of=duplicate,
        supersedes=supersedes,
        conflict_group_id=conflict_group,
        conflict_targets=(conflict_target,),
    )
    link = RelationDraft(target_id=uuid4(), relation_type="link", confidence=0.9)

    merged = merge_validated_actions(existing, (link,))

    assert merged.relations == (link,)
    assert merged.duplicate_of == duplicate
    assert merged.supersedes == supersedes
    assert merged.conflict_group_id == conflict_group
    assert merged.conflict_targets == (conflict_target,)
