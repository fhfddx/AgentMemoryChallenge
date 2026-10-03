"""确定性动作校验器单元测试。"""

from uuid import uuid4

import pytest

from masm.orchestration.action_validator import (
    ALLOWED_ACTIONS,
    ActionValidationError,
    validate_actions,
)
from masm.schemas.agents import ActionKind, CuratorAction, CuratorDecision
from masm.storage.types import MemoryCandidate

_USER = "user-1"


def _candidate(index: int = 0, **overrides: object) -> MemoryCandidate:
    payload: dict = {
        "memory_id": uuid4(),
        "user_id": _USER,
        "content": f"memory {index}",
        "score": 0.0,
    }
    payload.update(overrides)
    return MemoryCandidate(**payload)


def _action(kind: ActionKind, target=None, **overrides: object) -> CuratorAction:
    payload: dict = {"kind": kind, "confidence": 0.7, "evidence": "observed"}
    if target is not None:
        payload["target_memory_id"] = target
    payload.update(overrides)
    return CuratorAction(**payload)


def test_allowed_actions_are_the_five_management_actions() -> None:
    assert {kind.value for kind in ALLOWED_ACTIONS} == {
        "create",
        "link",
        "merge",
        "supersede",
        "conflict",
    }


def test_create_only_decision_yields_actions_without_relations() -> None:
    actions = validate_actions(_USER, CuratorDecision(actions=(_action(ActionKind.CREATE),)), [])

    assert actions.relations == ()
    assert actions.duplicate_of is None
    assert actions.supersedes is None
    assert actions.conflict_group_id is None


def test_link_creates_relation_to_candidate() -> None:
    candidate = _candidate()
    decision = CuratorDecision(actions=(_action(ActionKind.LINK, candidate.memory_id),))

    actions = validate_actions(_USER, decision, [candidate])

    assert len(actions.relations) == 1
    relation = actions.relations[0]
    assert relation.target_id == candidate.memory_id
    assert relation.relation_type == "link"
    assert relation.confidence == 0.7


def test_cross_user_relation_is_rejected() -> None:
    """跨用户关系必须被确定性拒绝（候选只包含同一用户的历史）。"""
    other_user_candidate = _candidate(user_id="user-2")
    decision = CuratorDecision(
        actions=(_action(ActionKind.LINK, other_user_candidate.memory_id),)
    )

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [other_user_candidate])


def test_message_memory_cannot_be_governance_target() -> None:
    message = _candidate(granularity="message", request_id="run-1", source_position=0)
    decision = CuratorDecision(
        actions=(_action(ActionKind.SUPERSEDE, message.memory_id),)
    )

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [message])


def test_target_outside_candidates_is_rejected() -> None:
    decision = CuratorDecision(actions=(_action(ActionKind.LINK, uuid4()),))

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [_candidate()])


def test_action_without_target_is_rejected_for_relational_kinds() -> None:
    for kind in (ActionKind.LINK, ActionKind.MERGE, ActionKind.SUPERSEDE, ActionKind.CONFLICT):
        with pytest.raises(ActionValidationError):
            validate_actions(_USER, CuratorDecision(actions=(_action(kind),)), [_candidate()])


def test_merge_sets_duplicate_of() -> None:
    candidate = _candidate()
    decision = CuratorDecision(actions=(_action(ActionKind.MERGE, candidate.memory_id),))

    actions = validate_actions(_USER, decision, [candidate])

    assert actions.duplicate_of == candidate.memory_id


def test_supersede_sets_supersedes() -> None:
    candidate = _candidate()
    decision = CuratorDecision(actions=(_action(ActionKind.SUPERSEDE, candidate.memory_id),))

    actions = validate_actions(_USER, decision, [candidate])

    assert actions.supersedes == candidate.memory_id


def test_conflict_assigns_shared_group() -> None:
    candidate = _candidate()
    decision = CuratorDecision(actions=(_action(ActionKind.CONFLICT, candidate.memory_id),))

    actions = validate_actions(_USER, decision, [candidate])

    assert actions.conflict_group_id is not None
    assert actions.conflict_targets == (candidate.memory_id,)


def test_conflict_continues_existing_group() -> None:
    """目标已属于某个冲突组时，新记忆必须加入同一组而不是新建组。"""
    group_id = uuid4()
    candidate = _candidate(conflict_group_id=group_id)
    decision = CuratorDecision(actions=(_action(ActionKind.CONFLICT, candidate.memory_id),))

    actions = validate_actions(_USER, decision, [candidate])

    assert actions.conflict_group_id == group_id


def test_multiple_existing_conflict_groups_are_rejected() -> None:
    """一次决策同时指向两个不同冲突组时必须确定性拒绝。"""
    first = _candidate(0, conflict_group_id=uuid4())
    second = _candidate(1, conflict_group_id=uuid4())
    decision = CuratorDecision(
        actions=(
            _action(ActionKind.CONFLICT, first.memory_id),
            _action(ActionKind.CONFLICT, second.memory_id),
        )
    )

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [first, second])


def test_multiple_conflict_targets_in_same_group_are_accepted() -> None:
    group_id = uuid4()
    first = _candidate(0, conflict_group_id=group_id)
    second = _candidate(1, conflict_group_id=group_id)
    decision = CuratorDecision(
        actions=(
            _action(ActionKind.CONFLICT, first.memory_id),
            _action(ActionKind.CONFLICT, second.memory_id),
        )
    )

    actions = validate_actions(_USER, decision, [first, second])

    assert actions.conflict_group_id == group_id


def test_cyclic_supersede_is_rejected() -> None:
    """循环替代必须被确定性拒绝。"""
    first = _candidate(0)
    second = _candidate(1, supersedes=first.memory_id)
    cyclic_first = MemoryCandidate(
        memory_id=first.memory_id,
        user_id=_USER,
        content=first.content,
        score=0.0,
        supersedes=second.memory_id,
    )
    decision = CuratorDecision(actions=(_action(ActionKind.SUPERSEDE, cyclic_first.memory_id),))

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [cyclic_first, second])


def test_acyclic_supersede_chain_is_accepted() -> None:
    root = _candidate(0)
    leaf = _candidate(1, supersedes=root.memory_id)
    decision = CuratorDecision(actions=(_action(ActionKind.SUPERSEDE, leaf.memory_id),))

    actions = validate_actions(_USER, decision, [root, leaf])

    assert actions.supersedes == leaf.memory_id


def test_evidence_rewrite_field_is_rejected_by_schema() -> None:
    """动作 Schema 不接受任何改写原始证据的字段。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CuratorAction(
            kind=ActionKind.CREATE,
            confidence=0.5,
            evidence="observed",
            original_text="rewritten",
        )


def test_duplicate_targets_across_actions_are_rejected() -> None:
    candidate = _candidate()
    decision = CuratorDecision(
        actions=(
            _action(ActionKind.MERGE, candidate.memory_id),
            _action(ActionKind.SUPERSEDE, candidate.memory_id),
        )
    )

    with pytest.raises(ActionValidationError):
        validate_actions(_USER, decision, [candidate])


def test_empty_decision_is_valid() -> None:
    actions = validate_actions(_USER, CuratorDecision(), [])

    assert actions.relations == ()
    assert actions.conflict_targets == ()
