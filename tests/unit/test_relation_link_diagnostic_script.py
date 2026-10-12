"""The relation-link diagnostic reports only aggregate stage evidence."""

import json
from uuid import uuid4

from scripts.relation_link_diagnostic import (
    LinkStageRecorder,
    ObservedHistoryBuilder,
    ObservedRelationLinker,
)

from masm.schemas.agents import (
    PerceptionResult,
    RelationLinkDecision,
    RelationLinkProposal,
)
from masm.schemas.api import AddRequest, Message
from masm.storage.types import MemoryCandidate, MemoryDraft


class _HistoryBuilder:
    def __init__(self, candidates: list[MemoryCandidate]) -> None:
        self._candidates = candidates

    def build(
        self, request: AddRequest, perception: PerceptionResult
    ) -> list[MemoryCandidate]:
        return list(self._candidates)


class _RelationLinker:
    def __init__(self, decision: RelationLinkDecision) -> None:
        self._decision = decision

    def propose(
        self, new_memory: MemoryDraft, history: list[MemoryCandidate]
    ) -> RelationLinkDecision:
        return self._decision


def _request() -> AddRequest:
    return AddRequest(
        request_id="diagnostic-request",
        user_id="diagnostic-user",
        session_id="diagnostic-session",
        messages=[Message(role="user", content="The bridge continues to the leaf.")],
    )


def test_observers_separate_history_proposal_and_validation_stages() -> None:
    target_id = uuid4()
    candidate = MemoryCandidate(
        memory_id=target_id,
        user_id="diagnostic-user",
        content="The root continues through the bridge.",
        score=1.0,
    )
    recorder = LinkStageRecorder()
    recorder.begin(user_id="diagnostic-user", expected_target_id=target_id)
    history = ObservedHistoryBuilder(_HistoryBuilder([candidate]), recorder).build(
        _request(), PerceptionResult()
    )
    decision = RelationLinkDecision(
        links=(
            RelationLinkProposal(
                target_memory_id=target_id,
                current_evidence="bridge continues to the leaf",
                target_evidence="root continues through the bridge",
                confidence=0.95,
            ),
        )
    )

    ObservedRelationLinker(_RelationLinker(decision), recorder).propose(
        MemoryDraft(
            summary="summary",
            original_text="The bridge continues to the leaf.",
        ),
        history,
    )

    assert recorder.safe_row(step=1, persisted_adjacency=True) == {
        "accepted_count": 1,
        "case": "relation_link_stage",
        "expected_target_accepted": True,
        "expected_target_in_history": True,
        "expected_target_proposed": True,
        "failure_category": None,
        "history_count": 1,
        "linker_called": True,
        "persisted_adjacency": True,
        "proposal_count": 1,
        "step": 1,
    }


def test_diagnostic_row_never_contains_content_or_identifiers() -> None:
    target_id = uuid4()
    secret_content = "private-marker-that-must-not-be-emitted"
    candidate = MemoryCandidate(
        memory_id=target_id,
        user_id="diagnostic-user",
        content=secret_content,
        score=1.0,
    )
    recorder = LinkStageRecorder()
    recorder.begin(user_id="diagnostic-user", expected_target_id=target_id)
    history = ObservedHistoryBuilder(_HistoryBuilder([candidate]), recorder).build(
        _request(), PerceptionResult()
    )
    decision = RelationLinkDecision(
        links=(
            RelationLinkProposal(
                target_memory_id=target_id,
                current_evidence="not present in current memory",
                target_evidence=secret_content,
                confidence=0.95,
            ),
        )
    )
    ObservedRelationLinker(_RelationLinker(decision), recorder).propose(
        MemoryDraft(summary="summary", original_text="different current content"),
        history,
    )

    serialized = json.dumps(recorder.safe_row(step=2, persisted_adjacency=False))

    assert secret_content not in serialized
    assert str(target_id) not in serialized
    assert '"accepted_count": 0' in serialized
    assert '"expected_target_proposed": true' in serialized
    assert '"expected_target_accepted": false' in serialized
