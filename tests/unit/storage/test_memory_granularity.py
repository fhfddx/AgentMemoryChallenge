"""双粒度记忆的领域类型契约。"""

from uuid import uuid4

from masm.storage.types import MemoryCandidate, MemoryDraft


def test_message_draft_keeps_source_position() -> None:
    draft = MemoryDraft(
        summary="甲在周二到达上海",
        original_text="甲在周二到达上海",
        granularity="message",
        source_position=2,
    )

    assert (draft.granularity, draft.source_position) == ("message", 2)


def test_candidate_keeps_provenance_for_retrieval() -> None:
    candidate = MemoryCandidate(
        memory_id=uuid4(),
        user_id="user-a",
        content="甲在周二到达上海",
        score=0.5,
        granularity="message",
        request_id="run-1",
        source_position=2,
    )

    assert (candidate.granularity, candidate.request_id, candidate.source_position) == (
        "message",
        "run-1",
        2,
    )
