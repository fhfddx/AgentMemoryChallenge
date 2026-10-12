"""Deterministic validation for optional Add-time relation links."""

import math
from collections.abc import Sequence

from masm.schemas.agents import RelationLinkDecision
from masm.storage.types import MemoryCandidate, RelationDraft, ValidatedActions

MIN_LINK_CONFIDENCE = 0.85
MAX_LINKS = 4


def _normalized_quote(value: str) -> str:
    return " ".join(value.split())


def validate_relation_links(
    user_id: str,
    decision: RelationLinkDecision,
    current_text: str,
    candidates: Sequence[MemoryCandidate],
    *,
    min_confidence: float = MIN_LINK_CONFIDENCE,
    max_links: int = MAX_LINKS,
) -> tuple[RelationDraft, ...]:
    """Admit only grounded links to supplied same-user context memories."""
    threshold = float(min_confidence)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("min_confidence must be a finite value in [0, 1]")
    if not 0 <= max_links <= MAX_LINKS:
        raise ValueError(f"max_links must be in [0, {MAX_LINKS}]")

    by_id = {
        candidate.memory_id: candidate
        for candidate in candidates
        if candidate.user_id == user_id and candidate.granularity == "context"
    }
    normalized_current = _normalized_quote(current_text)
    accepted: list[RelationDraft] = []
    seen_targets = set()
    for proposal in decision.links:
        if len(accepted) >= max_links:
            break
        candidate = by_id.get(proposal.target_memory_id)
        if candidate is None or proposal.target_memory_id in seen_targets:
            continue
        if proposal.confidence < threshold:
            continue
        current_evidence = _normalized_quote(proposal.current_evidence)
        target_evidence = _normalized_quote(proposal.target_evidence)
        if not current_evidence or current_evidence not in normalized_current:
            continue
        if not target_evidence or target_evidence not in _normalized_quote(candidate.content):
            continue
        seen_targets.add(proposal.target_memory_id)
        accepted.append(
            RelationDraft(
                target_id=proposal.target_memory_id,
                relation_type="link",
                confidence=proposal.confidence,
                evidence_ref={
                    "kind": "link",
                    "current_evidence": proposal.current_evidence,
                    "target_evidence": proposal.target_evidence,
                },
            )
        )
    return tuple(accepted)


def merge_validated_actions(
    actions: ValidatedActions, links: Sequence[RelationDraft]
) -> ValidatedActions:
    """Append generic links unless Curator already owns the target."""
    existing_targets = {relation.target_id for relation in actions.relations}
    merged_relations = list(actions.relations)
    for link in links:
        if link.target_id in existing_targets:
            continue
        existing_targets.add(link.target_id)
        merged_relations.append(link)
    return ValidatedActions(
        relations=tuple(merged_relations),
        duplicate_of=actions.duplicate_of,
        supersedes=actions.supersedes,
        conflict_group_id=actions.conflict_group_id,
        conflict_targets=actions.conflict_targets,
    )
