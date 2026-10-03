"""Agent Add 编排流水线的端到端集成测试。"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from masm.agents import MAX_HISTORY
from masm.agents.curator import MemoryCuratorAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.orchestration.action_validator import ActionValidationError, validate_actions
from masm.orchestration.add_pipeline import AddPipeline, PipelineState
from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever, ParsedQuery
from masm.schemas.agents import (
    ActionKind,
    CuratorAction,
    CuratorDecision,
    PerceptionResult,
    TemporalRelationResult,
)
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryConflict, MemoryRelation, SourceMessage
from masm.storage.repositories import MemoryRepository

_FULL_TRACE = (
    PipelineState.CREATED,
    PipelineState.PARSED,
    PipelineState.PERCEIVED,
    PipelineState.RECALLED,
    PipelineState.TEMPORALISED,
    PipelineState.CURATED,
    PipelineState.VALIDATED,
    PipelineState.COMPLETED,
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _action(kind: ActionKind, target: UUID | None = None) -> CuratorAction:
    payload: dict = {"kind": kind, "confidence": 0.8, "evidence": "observed evidence"}
    if target is not None:
        payload["target_memory_id"] = target
    return CuratorAction(**payload)


def _decision(kind: ActionKind, target: UUID | None = None) -> CuratorDecision:
    return CuratorDecision(actions=(_action(kind, target),))


def _build_pipeline(
    database: Database,
    embeddings,
    *,
    decision: CuratorDecision | None = None,
    max_history: int = 8,
) -> tuple[AddPipeline, tuple[FakeStructuredLLM, ...]]:
    perception_llm = FakeStructuredLLM([PerceptionResult(keywords=("cat",), language="en")])
    temporal_llm = FakeStructuredLLM([TemporalRelationResult()])
    curator_llm = FakeStructuredLLM(
        [decision if decision is not None else _decision(ActionKind.CREATE)]
    )
    pipeline = AddPipeline(
        perception=PerceptionAgent(perception_llm),
        temporal=TemporalRelationAgent(temporal_llm, max_history=max_history),
        curator=MemoryCuratorAgent(curator_llm, max_history=max_history),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
        max_history=max_history,
    )
    return pipeline, (perception_llm, temporal_llm, curator_llm)


def _count(database: Database, model: type, user_id: str) -> int:
    with database.session() as session:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )


def _memory_row(database: Database, user_id: str, request_id: str) -> Memory:
    with database.session() as session:
        return session.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
                Memory.granularity == "context",
            )
        ).scalar_one()


def test_agent_mode_add_is_immediately_searchable(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    pipeline, _llms = _build_pipeline(database, embeddings)
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    search = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    )
    user_id = _uid("u")
    response = service.add(_request(_uid("r"), user_id, "the cat sat on the mat"))

    assert response.success is True
    assert MemoryRepository(database).get_ledger_status(user_id, response.request_id) == "COMMITTED"

    results = search.search(SearchRequest(query="cat", user_id=user_id, top_k=10))
    assert results.data


def test_state_machine_visits_states_in_order(
    database: Database, embeddings
) -> None:
    pipeline, _llms = _build_pipeline(database, embeddings)
    user_id = _uid("u")

    result = pipeline.run(_request(_uid("r"), user_id, "ordered states"))

    assert result.states == _FULL_TRACE
    assert result.degraded is False


def test_agents_are_invoked_exactly_once(database: Database, embeddings) -> None:
    """显式状态机：不得递归调用智能体。"""
    pipeline, llms = _build_pipeline(database, embeddings)

    pipeline.run(_request(_uid("r"), _uid("u"), "no recursion"))

    for llm in llms:
        assert len(llm.requests) == 1


def test_history_candidates_are_capped(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    for index in range(6):
        baseline.add(_request(_uid("r"), user_id, f"history memory {index}"))

    pipeline, llms = _build_pipeline(database, embeddings, max_history=2)
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "history memory"))

    _perception_llm, _temporal_llm, curator_llm = llms
    assert len(curator_llm.requests[0].payload["history"]) <= 2


def test_link_action_persists_relation(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "the cat sat on the mat"))
    target = _memory_row(database, user_id, _lookup_request_id(database, user_id))

    pipeline, _llms = _build_pipeline(
        database, embeddings, decision=_decision(ActionKind.LINK, target.id)
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "the cat sat on the mat again"))

    with database.session() as session:
        relations = list(
            session.execute(
                select(MemoryRelation).where(MemoryRelation.user_id == user_id)
            ).scalars()
        )
    assert len(relations) == 1
    assert relations[0].target_id == target.id
    assert relations[0].relation_type == "link"


def test_governance_actions_apply_only_to_context(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """消息记忆不成为治理关系的源节点。"""
    user_id = _uid("u")
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    baseline.add(_request(_uid("r"), user_id, "Kyoto is in Japan"))
    with database.session() as session:
        target_id = session.execute(
            select(Memory.id).where(Memory.user_id == user_id, Memory.granularity == "context")
        ).scalar_one()

    pipeline, _llms = _build_pipeline(
        database, embeddings, decision=_decision(ActionKind.LINK, target_id)
    )
    request_id = _uid("r")
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(
        AddRequest(
            request_id=request_id,
            user_id=user_id,
            session_id="session-2",
            messages=[
                {"role": "user", "content": "Alice visited Kyoto"},
                {"role": "assistant", "content": "She admired the gardens"},
            ],
        )
    )

    with database.session() as session:
        rows = session.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
            )
        ).scalars().all()
        relations = session.execute(
            select(MemoryRelation).where(MemoryRelation.user_id == user_id)
        ).scalars().all()
    assert len(rows) == 3
    assert len(relations) == 1
    assert relations[0].source_id == rows[0].id
    assert rows[0].granularity == "context"
    assert all(row.granularity == "message" and row.supersedes is None for row in rows[1:])


def test_governance_history_uses_only_context_anchors(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    baseline.add(_request(_uid("r"), user_id, "Kyoto garden memory"))
    pipeline, _llms = _build_pipeline(database, embeddings)

    history = pipeline._recall(_request(_uid("r"), user_id, "Kyoto garden memory"))

    assert history
    assert all(candidate.granularity == "context" for candidate in history)


def test_governance_history_fills_distinct_contexts_after_message_collapse(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    first_run, second_run = _uid("r"), _uid("r")
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    baseline.add(AddRequest(
        request_id=first_run, user_id=user_id, session_id="session-1",
        messages=[{"role": "user", "content": f"needle {index}"} for index in range(8)],
    ))
    baseline.add(_request(second_run, user_id, "needle other session"))
    pipeline, _llms = _build_pipeline(database, embeddings)

    history = pipeline._recall(_request(_uid("r"), user_id, "needle"))

    assert {item.request_id for item in history} == {first_run, second_run}
    assert all(item.granularity == "context" for item in history)


def _lookup_request_id(database: Database, user_id: str) -> str:
    with database.session() as session:
        return str(
            session.execute(
                select(Memory.request_id).where(Memory.user_id == user_id).limit(1)
            ).scalar_one()
        )


def test_supersede_marks_target_without_touching_evidence(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "the cat was orange"))
    target = _memory_row(database, user_id, _lookup_request_id(database, user_id))
    original_evidence = _evidence_snapshot(database, user_id)

    pipeline, _llms = _build_pipeline(
        database,
        embeddings,
        decision=CuratorDecision(actions=(_action(ActionKind.SUPERSEDE, target.id),)),
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "the cat is now black"))

    with database.session() as session:
        refreshed = session.execute(
            select(Memory).where(Memory.id == target.id)
        ).scalar_one()
        superseding = session.execute(
            select(Memory).where(Memory.supersedes == target.id)
        ).scalars().all()

    assert refreshed.status == "superseded"
    assert len(superseding) == 1
    # 原始证据不可变：source_messages 与既有记忆正文逐字未变。
    assert _evidence_snapshot(database, user_id)[: len(original_evidence)] == original_evidence
    assert refreshed.original_text == target.original_text
    assert refreshed.summary == target.summary


def _evidence_snapshot(database: Database, user_id: str) -> list[tuple]:
    with database.session() as session:
        return [
            (row.id, row.position, row.role, row.content, row.created_at)
            for row in session.execute(
                select(SourceMessage)
                .where(SourceMessage.user_id == user_id)
                .order_by(SourceMessage.position, SourceMessage.created_at)
            ).scalars()
        ]


def test_merge_sets_duplicate_of(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "duplicate source"))
    target = _memory_row(database, user_id, _lookup_request_id(database, user_id))

    pipeline, _llms = _build_pipeline(
        database, embeddings, decision=_decision(ActionKind.MERGE, target.id)
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "duplicate source"))

    with database.session() as session:
        merged = session.execute(
            select(Memory).where(Memory.duplicate_of == target.id)
        ).scalars().all()
    assert len(merged) == 1


def test_conflict_creates_group_and_conflict_rows(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "the meeting is on Monday"))
    target = _memory_row(database, user_id, _lookup_request_id(database, user_id))

    pipeline, _llms = _build_pipeline(
        database,
        embeddings,
        decision=CuratorDecision(actions=(_action(ActionKind.CONFLICT, target.id),)),
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "the meeting is on Tuesday"))

    with database.session() as session:
        refreshed = session.execute(select(Memory).where(Memory.id == target.id)).scalar_one()
        conflicting = session.execute(
            select(Memory).where(Memory.conflict_group_id == refreshed.conflict_group_id)
        ).scalars().all()
        conflicts = list(
            session.execute(
                select(MemoryConflict).where(MemoryConflict.user_id == user_id)
            ).scalars()
        )

    assert refreshed.conflict_group_id is not None
    assert len(conflicting) == 2
    assert len(conflicts) == 2
    assert {row.memory_id for row in conflicts} == {row.id for row in conflicting}


def _memory_ids(database: Database, user_id: str) -> list[UUID]:
    with database.session() as session:
        return list(
            session.execute(
                select(Memory.id)
                .where(Memory.user_id == user_id, Memory.granularity == "context")
                .order_by(Memory.created_at, Memory.id)
            ).scalars()
        )


def _evidence_map(database: Database, user_id: str) -> dict:
    with database.session() as session:
        return {
            row.id: (row.position, row.role, row.content)
            for row in session.execute(
                select(SourceMessage).where(SourceMessage.user_id == user_id)
            ).scalars()
        }


def _set_supersede(database: Database, source_id: UUID, target_id: UUID) -> None:
    with database.session() as session:
        session.execute(
            update(Memory).where(Memory.id == source_id).values(supersedes=target_id)
        )
        session.commit()


def _retrieve_history(database: Database, embeddings, user_id: str, text: str):
    return BaselineRetriever(
        MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
    ).retrieve(user_id, ParsedQuery(text_queries=(text,), intent="fact"), 10)


def _add_with_decision(
    database, asset_store, settings, embeddings, user_id, request_id, text, kind, target
):
    pipeline, _llms = _build_pipeline(database, embeddings, decision=_decision(kind, target))
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    return service.add(_request(request_id, user_id, text))


def test_real_recall_preserves_governance_fields(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """真实召回链路必须完整保留 supersedes / status / conflict_group_id。"""
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "governed memory alpha"))
    baseline.add(_request(_uid("r"), user_id, "governed memory beta"))
    first_id, second_id = _memory_ids(database, user_id)
    _set_supersede(database, first_id, second_id)
    group_id = uuid4()
    with database.session() as session:
        session.execute(
            update(Memory)
            .where(Memory.id == first_id)
            .values(status="superseded", conflict_group_id=group_id)
        )
        session.commit()

    by_id = {
        candidate.memory_id: candidate
        for candidate in _retrieve_history(database, embeddings, user_id, "governed memory")
    }

    assert by_id[first_id].supersedes == second_id
    assert by_id[first_id].status == "superseded"
    assert by_id[first_id].conflict_group_id == group_id


def test_supersede_cycle_is_rejected_through_real_recall_path(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """Repository 通道 -> BaselineRetriever -> validate_actions：替代环必须让 SUPERSEDE 被拒绝。"""
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    baseline.add(_request(_uid("r"), user_id, "cycle memory one"))
    baseline.add(_request(_uid("r"), user_id, "cycle memory two"))
    first_id, second_id = _memory_ids(database, user_id)
    _set_supersede(database, first_id, second_id)
    _set_supersede(database, second_id, first_id)

    history = _retrieve_history(database, embeddings, user_id, "cycle memory")

    assert {candidate.memory_id for candidate in history} >= {first_id, second_id}
    assert any(candidate.supersedes is not None for candidate in history)

    decision = CuratorDecision(actions=(_action(ActionKind.SUPERSEDE, first_id),))
    with pytest.raises(ActionValidationError):
        validate_actions(user_id, decision, history)


def test_conflict_group_continues_across_decisions(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """A 与 B 成组后 C 与 A 冲突：A/B/C 同一组，每成员恰好一条记录，原始证据不变。"""
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    request_a = _uid("r")
    baseline.add(_request(request_a, user_id, "the meeting is on Monday"))
    memory_a = _memory_row(database, user_id, request_a)

    request_b = _uid("r")
    _add_with_decision(
        database, asset_store, settings, embeddings, user_id, request_b,
        "the meeting is on Tuesday", ActionKind.CONFLICT, memory_a.id,
    )
    memory_b = _memory_row(database, user_id, request_b)
    evidence_before = _evidence_map(database, user_id)

    request_c = _uid("r")
    _add_with_decision(
        database, asset_store, settings, embeddings, user_id, request_c,
        "the meeting is on Wednesday", ActionKind.CONFLICT, memory_a.id,
    )
    memory_c = _memory_row(database, user_id, request_c)

    member_ids = [memory_a.id, memory_b.id, memory_c.id]
    with database.session() as session:
        groups = {
            row.id: row.conflict_group_id
            for row in session.execute(
                select(Memory).where(Memory.id.in_(member_ids))
            ).scalars()
        }
        conflicts = list(
            session.execute(
                select(MemoryConflict).where(MemoryConflict.user_id == user_id)
            ).scalars()
        )

    assert set(groups) == set(member_ids)
    assert len(set(groups.values())) == 1
    assert None not in groups.values()

    assert len(conflicts) == 3
    assert {row.memory_id for row in conflicts} == set(member_ids)
    assert {row.conflict_group_id for row in conflicts} == set(groups.values())
    assert len({row.version for row in conflicts}) == 3

    evidence_after = _evidence_map(database, user_id)
    assert {key: value for key, value in evidence_after.items() if key in evidence_before} == (
        evidence_before
    )


def test_pipeline_history_never_exceeds_hard_cap(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """超大配置被安全截断，传给模型的历史绝不超过硬上限。"""
    baseline = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    user_id = _uid("u")
    for index in range(MAX_HISTORY + 4):
        baseline.add(_request(_uid("r"), user_id, f"hard cap memory {index}"))

    pipeline, llms = _build_pipeline(database, embeddings, max_history=10_000)
    assert pipeline.max_history == MAX_HISTORY
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    service.add(_request(_uid("r"), user_id, "hard cap memory"))

    _perception_llm, temporal_llm, curator_llm = llms
    assert len(temporal_llm.requests[0].payload["history"]) <= MAX_HISTORY
    assert len(curator_llm.requests[0].payload["history"]) <= MAX_HISTORY


def test_zero_history_pipeline_still_adds(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """max_history=0 是合法配置：不召回历史，但 Add 仍然成功。"""
    pipeline, llms = _build_pipeline(database, embeddings, max_history=0)
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    user_id = _uid("u")

    response = service.add(_request(_uid("r"), user_id, "no history memory"))

    assert response.success is True
    _perception_llm, temporal_llm, curator_llm = llms
    assert temporal_llm.requests[0].payload["history"] == []
    assert curator_llm.requests[0].payload["history"] == []


def test_source_message_count_matches_request(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    pipeline, _llms = _build_pipeline(database, embeddings)
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    user_id = _uid("u")

    service.add(_request(_uid("r"), user_id, "one message"))

    assert _count(database, SourceMessage, user_id) == 1
    assert _count(database, Memory, user_id) == 2
