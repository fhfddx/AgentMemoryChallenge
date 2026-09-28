"""Agent Add 编排流水线的端到端集成测试。"""

from uuid import UUID, uuid4

from sqlalchemy import func, select

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.orchestration.add_pipeline import AddPipeline, PipelineState
from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
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
            select(Memory).where(Memory.user_id == user_id, Memory.request_id == request_id)
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
    assert _count(database, Memory, user_id) == 1
