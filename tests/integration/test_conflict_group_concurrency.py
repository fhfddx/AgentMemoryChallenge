"""并发创建/扩展冲突组的 PostgreSQL 确定性并发测试（barrier 控制事务交错）。"""

import itertools
import threading
from uuid import UUID, uuid4

from sqlalchemy import select

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
from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryConflict
from masm.storage.repositories import MemoryRepository

_HOOK_TIMEOUT = 10.0


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str, session_id: str = "session-1") -> AddRequest:
    """并发请求使用不同 session_id，避免 sessions 行 upsert 成为额外串行点。"""
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        messages=[{"role": "user", "content": text}],
    )


def _decision(target: UUID) -> CuratorDecision:
    return CuratorDecision(
        actions=(
            CuratorAction(
                kind=ActionKind.CONFLICT,
                target_memory_id=target,
                confidence=0.8,
                evidence="observed evidence",
            ),
        )
    )


def _service(database, asset_store, settings, embeddings, target: UUID) -> AddService:
    pipeline = AddPipeline(
        perception=PerceptionAgent(FakeStructuredLLM([PerceptionResult(keywords=("meeting",))])),
        temporal=TemporalRelationAgent(FakeStructuredLLM([TemporalRelationResult()])),
        curator=MemoryCuratorAgent(FakeStructuredLLM([_decision(target)])),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
    )
    return AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )


def _seed(database, asset_store, settings, embeddings, user_id: str, request_id: str) -> UUID:
    AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings
    ).add(_request(request_id, user_id, "the meeting is on Monday"))
    return _memory_row(database, user_id, request_id).id


def _memory_row(database: Database, user_id: str, request_id: str) -> Memory:
    with database.session() as session:
        return session.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.request_id == request_id)
        ).scalar_one()


def _serializing_hook(original, first_locked: threading.Event):
    """让先到的事务拿到锁后等待，后到的事务进入后再继续，形成确定交错。"""
    counter = itertools.count()
    order_lock = threading.Lock()
    arrived = threading.Event()

    def _hook(self, session, user_id, target_ids):
        with order_lock:
            is_first = next(counter) == 0
        if not is_first:
            arrived.set()
            return original(self, session, user_id, target_ids)
        locked = original(self, session, user_id, target_ids)
        assert arrived.wait(timeout=_HOOK_TIMEOUT), "第二个事务未进入加锁阶段"
        first_locked.set()
        return locked

    return _hook


def _run_concurrently(
    services: list[AddService], requests: list[AddRequest]
) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(len(services))

    def _worker(service: AddService, request: AddRequest) -> None:
        try:
            start.wait()
            service.add(request)
        except BaseException as exc:  # noqa: BLE001 - 记录并发期间的任何异常
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(service, request))
        for service, request in zip(services, requests, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads), "并发请求未在超时内结束"
    return errors


def _assert_single_group(database: Database, user_id: str, member_ids: list[UUID]) -> None:
    with database.session() as session:
        groups = {
            row.id: row.conflict_group_id
            for row in session.execute(
                select(Memory).where(Memory.id.in_(member_ids))
            ).scalars()
        }
        rows = list(
            session.execute(
                select(MemoryConflict).where(MemoryConflict.user_id == user_id)
            ).scalars()
        )
    assert set(groups) == set(member_ids)
    assert len(set(groups.values())) == 1, "冲突组被拆分"
    assert None not in groups.values()
    assert {row.memory_id for row in rows} == set(member_ids), "存在孤立或缺失的成员"
    assert len(rows) == len(member_ids), "成员记录重复"
    assert len({row.version for row in rows}) == len(member_ids), "版本号重复"
    assert {row.conflict_group_id for row in rows} == set(groups.values())


def test_concurrent_conflicts_on_ungrouped_memory_join_one_group(
    monkeypatch, database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """两个请求同时与尚未分组的 A 冲突：最终必须同属一个组。"""
    user_id = _uid("u")
    request_a = _uid("r")
    memory_a = _seed(database, asset_store, settings, embeddings, user_id, request_a)

    locked = threading.Event()
    original = MemoryRepository._lock_action_targets
    monkeypatch.setattr(
        MemoryRepository, "_lock_action_targets", _serializing_hook(original, locked)
    )

    request_b, request_c = _uid("r"), _uid("r")
    services = [
        _service(database, asset_store, settings, embeddings, memory_a),
        _service(database, asset_store, settings, embeddings, memory_a),
    ]
    errors = _run_concurrently(
        services,
        [
            _request(request_b, user_id, "the meeting is on Monday", "session-b"),
            _request(request_c, user_id, "the meeting is on Monday", "session-c"),
        ],
    )

    assert errors == []
    assert locked.is_set(), "先到的事务未取得目标行锁"
    memory_b = _memory_row(database, user_id, request_b).id
    memory_c = _memory_row(database, user_id, request_c).id
    _assert_single_group(database, user_id, [memory_a, memory_b, memory_c])


def test_concurrent_appends_to_existing_group_keep_versions_unique(
    monkeypatch, database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """两个请求经同一已有组的不同目标并发追加：不得重复版本或拆组。"""
    user_id = _uid("u")
    request_a = _uid("r")
    memory_a = _seed(database, asset_store, settings, embeddings, user_id, request_a)

    # 先建立包含 A 与 B 的冲突组。
    request_b = _uid("r")
    _service(database, asset_store, settings, embeddings, memory_a).add(
        _request(request_b, user_id, "the meeting is on Tuesday")
    )
    memory_b = _memory_row(database, user_id, request_b).id

    locked = threading.Event()
    original = MemoryRepository._lock_conflict_group
    monkeypatch.setattr(
        MemoryRepository, "_lock_conflict_group", _serializing_hook(original, locked)
    )

    request_c, request_d = _uid("r"), _uid("r")
    services = [
        _service(database, asset_store, settings, embeddings, memory_a),
        _service(database, asset_store, settings, embeddings, memory_b),
    ]
    errors = _run_concurrently(
        services,
        [
            _request(request_c, user_id, "the meeting is on Monday", "session-c"),
            _request(request_d, user_id, "the meeting is on Monday", "session-d"),
        ],
    )

    assert errors == []
    assert locked.is_set(), "先到的事务未取得冲突组行锁"
    memory_c = _memory_row(database, user_id, request_c).id
    memory_d = _memory_row(database, user_id, request_d).id
    _assert_single_group(database, user_id, [memory_a, memory_b, memory_c, memory_d])
    with database.session() as session:
        versions = sorted(
            row.version
            for row in session.execute(
                select(MemoryConflict).where(MemoryConflict.user_id == user_id)
            ).scalars()
        )
    assert versions == [1, 2, 3, 4]
