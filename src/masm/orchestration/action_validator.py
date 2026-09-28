"""确定性记忆管理动作校验。

校验器是智能体输出与数据库之间唯一的闸门：跨用户目标、未知动作、循环替代与任何改写
原始证据的企图都在这里被确定性拒绝。校验后的结果只包含治理字段与关系边。
"""

from collections.abc import Sequence
from uuid import UUID, uuid4

from masm.schemas.agents import ActionKind, CuratorDecision
from masm.storage.types import MemoryCandidate, RelationDraft, ValidatedActions

# 允许的动作集合：只有设计规范 6.3 列出的五种。
ALLOWED_ACTIONS = frozenset(ActionKind)

_RELATIONAL_ACTIONS = frozenset(
    {ActionKind.LINK, ActionKind.MERGE, ActionKind.SUPERSEDE, ActionKind.CONFLICT}
)

_RELATION_TYPES: dict[ActionKind, str] = {
    ActionKind.LINK: "link",
    ActionKind.MERGE: "duplicate",
    ActionKind.SUPERSEDE: "supersede",
    ActionKind.CONFLICT: "conflict",
}


class ActionValidationError(ValueError):
    """动作无法通过确定性校验。"""


def validate_actions(
    user_id: str,
    decision: CuratorDecision,
    candidates: Sequence[MemoryCandidate],
) -> ValidatedActions:
    """把智能体决策转换为可执行动作，任何违规都被拒绝。"""
    # 候选是同一用户范围内的召回结果；不在其中即跨用户或不存在。
    allowed_ids = {candidate.memory_id for candidate in candidates if candidate.user_id == user_id}

    relations: list[RelationDraft] = []
    duplicate_of: UUID | None = None
    supersedes: UUID | None = None
    conflict_targets: list[UUID] = []
    seen_targets: set[UUID] = set()

    for action in decision.actions:
        if action.kind not in ALLOWED_ACTIONS:
            raise ActionValidationError(f"不允许的动作类型: {action.kind}")
        if action.kind not in _RELATIONAL_ACTIONS:
            continue

        target = action.target_memory_id
        if target is None:
            raise ActionValidationError("关系类动作必须指定 target_memory_id")
        if target not in allowed_ids:
            raise ActionValidationError("目标记忆不属于该用户或不在本次候选范围内")
        if target in seen_targets:
            raise ActionValidationError("同一目标不得在同一决策中被重复管理")
        seen_targets.add(target)

        relations.append(
            RelationDraft(
                target_id=target,
                relation_type=_RELATION_TYPES[action.kind],
                confidence=action.confidence,
                evidence_ref={"kind": action.kind.value, "evidence": action.evidence},
            )
        )

        if action.kind is ActionKind.MERGE:
            duplicate_of = target
        elif action.kind is ActionKind.SUPERSEDE:
            if _has_supersede_cycle(target, candidates):
                raise ActionValidationError("循环替代被拒绝")
            supersedes = target
        elif action.kind is ActionKind.CONFLICT:
            conflict_targets.append(target)

    return ValidatedActions(
        relations=tuple(relations),
        duplicate_of=duplicate_of,
        supersedes=supersedes,
        conflict_group_id=uuid4() if conflict_targets else None,
        conflict_targets=tuple(conflict_targets),
    )


def _has_supersede_cycle(target: UUID, candidates: Sequence[MemoryCandidate]) -> bool:
    """目标所在的替代链若已存在环，则拒绝继续扩展该链。"""
    links = {candidate.memory_id: candidate.supersedes for candidate in candidates}
    seen: set[UUID] = set()
    current: UUID | None = target
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        current = links.get(current)
    return False
