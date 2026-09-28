"""治理动作失败时的降级事务测试。"""

import itertools
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.orchestration.add_pipeline import AddPipeline
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
from masm.services.add_service import AddConflictError, AddService, utc_now
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryConflict, MemoryRelation, SourceMessage
from masm.storage.repositories import (
    ActionApplicationError,
    MemoryRepository,
)
from masm.storage.types import MemoryBundle, MemoryDraft


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _decision(kind: ActionKind, target: UUID) -> CuratorDecision:
    return CuratorDecision(
        actions=(
            CuratorAction(
                kind=kind,
                target_memory_id=target,
                confidence=0.8,
                evidence="observed evidence",
            ),
        )
    )


def _pipeline(database: Database, embeddings, decision: CuratorDecision) -> AddPipeline:
    return AddPipeline(
        perception=PerceptionAgent(FakeStructuredLLM([PerceptionResult(keywords=("meeting",))])),
        temporal=TemporalRelationAgent(FakeStructuredLLM([TemporalRelationResult()])),
        curator=MemoryCuratorAgent(FakeStructuredLLM([decision])),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
    )


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


def _seed_target(database, asset_store, settings, embeddings, user_id: str) -> tuple[str, UUID]:
    request_id = _uid("r")
    AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings
    ).add(_request(request_id, user_id, "the meeting is on Monday"))
    return request_id, _memory_row(database, user_id, request_id).id


def test_governance_failure_degrades_to_committed_baseline(
    monkeypatch, database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """claim 成功后治理动作失败：必须用同一 owner token 降级提交基线记忆。"""
    user_id = _uid("u")
    _target_request, target_id = _seed_target(database, asset_store, settings, embeddings, user_id)

    def _explode(self, session, uid, target_ids):
        raise ActionApplicationError("injected governance failure")

    monkeypatch.setattr(MemoryRepository, "_lock_action_targets", _explode)
    pipeline = _pipeline(database, embeddings, _decision(ActionKind.CONFLICT, target_id))
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    request_id = _uid("r")

    response = service.add(_request(request_id, user_id, "the meeting is on Tuesday"))

    assert response.success is True
    repo = MemoryRepository(database)
    assert repo.get_ledger_status(user_id, request_id) == "COMMITTED"
    assert _count(database, Memory, user_id) == 2
    assert _count(database, SourceMessage, user_id) == 2

    # 不安全治理动作不得落库。
    assert _count(database, MemoryRelation, user_id) == 0
    assert _count(database, MemoryConflict, user_id) == 0
    degraded = _memory_row(database, user_id, request_id)
    assert degraded.conflict_group_id is None
    assert degraded.supersedes is None
    assert degraded.duplicate_of is None
    assert degraded.keywords == []

    # 基线记忆必须可检索。
    results = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    ).search(SearchRequest(query="meeting", user_id=user_id, top_k=10))
    assert len(results.data) == 2

    # 相同 request_id 重试必须回放已提交结果。
    retry = service.add(_request(request_id, user_id, "the meeting is on Tuesday"))
    assert retry == response
    assert _count(database, Memory, user_id) == 2


def test_governance_failure_with_missing_target_degrades(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """真实链路：目标在召回后消失，治理动作失败也必须降级为可检索基线。"""
    user_id = _uid("u")
    _target_request, target_id = _seed_target(database, asset_store, settings, embeddings, user_id)
    pipeline = _pipeline(database, embeddings, _decision(ActionKind.LINK, target_id))
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )

    original_finalize = MemoryRepository.finalize_request

    def _delete_target_then_finalize(self, uid, rid, bundle, *, owner_token):
        with self._database.session() as session:
            row = session.get(Memory, target_id)
            if row is not None:
                session.delete(row)
            session.commit()
        return original_finalize(self, uid, rid, bundle, owner_token=owner_token)

    MemoryRepository.finalize_request = _delete_target_then_finalize  # type: ignore[method-assign]
    request_id = _uid("r")
    try:
        response = service.add(_request(request_id, user_id, "the meeting is on Wednesday"))
    finally:
        MemoryRepository.finalize_request = original_finalize  # type: ignore[method-assign]

    assert response.success is True
    assert MemoryRepository(database).get_ledger_status(user_id, request_id) == "COMMITTED"
    assert _count(database, MemoryRelation, user_id) == 0


def test_governance_failure_after_takeover_does_not_touch_new_owner(
    monkeypatch, database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """降级时所有权已被接管：返回冲突，且绝不修改新 owner 的账本或数据。"""
    user_id = _uid("u")
    _target_request, target_id = _seed_target(database, asset_store, settings, embeddings, user_id)

    governance_failed = threading.Event()
    takeover_done = threading.Event()
    original_finalize = MemoryRepository.finalize_request

    attempts = itertools.count()

    def _fail_governance_first(self, uid, rid, bundle, *, owner_token):
        # 仅第一次（带治理动作）的提交失败；降级提交走真实实现。
        if next(attempts) > 0:
            return original_finalize(self, uid, rid, bundle, owner_token=owner_token)
        governance_failed.set()
        assert takeover_done.wait(timeout=10)
        raise ActionApplicationError("injected governance failure")

    monkeypatch.setattr(MemoryRepository, "finalize_request", _fail_governance_first)
    pipeline = _pipeline(database, embeddings, _decision(ActionKind.CONFLICT, target_id))
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    request_id = _uid("r")
    outcomes: dict[str, object] = {}

    def _slow_add() -> None:
        try:
            outcomes["response"] = service.add(
                _request(request_id, user_id, "the meeting is on Thursday")
            )
        except AddConflictError as exc:
            outcomes["conflict"] = exc

    thread = threading.Thread(target=_slow_add)
    thread.start()
    assert governance_failed.wait(timeout=10)

    # 新 owner 接管并提交同一 request_id。
    repo = MemoryRepository(database)
    clock = utc_now()
    new_token = repo.takeover_request(
        user_id,
        request_id,
        now=clock + timedelta(seconds=120),
        stale_before=clock + timedelta(seconds=60),
    )
    assert new_token is not None
    original_finalize(
        repo,
        user_id,
        request_id,
        MemoryBundle(
            session_id="session-1",
            request_id=request_id,
            memories=(
                MemoryDraft(summary="new owner baseline", original_text="new owner baseline"),
            ),
        ),
        owner_token=new_token,
    )
    takeover_done.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert "conflict" in outcomes
    assert "response" not in outcomes

    state = repo.get_ledger(user_id, request_id)
    assert state is not None
    assert state.status == "COMMITTED"
    assert state.owner_token == new_token

    # 新 owner 的数据未被改动：新增记忆没有治理字段，也没有冲突成员记录。
    assert _count(database, MemoryConflict, user_id) == 0
    assert _count(database, MemoryRelation, user_id) == 0
    with database.session() as session:
        governed = session.execute(
            select(Memory).where(
                Memory.user_id == user_id, Memory.conflict_group_id.is_not(None)
            )
        ).scalars().all()
    assert governed == []
