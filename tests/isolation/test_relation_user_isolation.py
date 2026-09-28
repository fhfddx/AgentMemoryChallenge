"""关系扩展的跨用户隔离测试（发布阻断项）。

即使数据库中异常存在跨用户关系边，关系读取也必须在 SQL 阶段按 user_id 过滤，
绝不把另一用户的记忆返回给当前用户。
"""

from uuid import UUID, uuid4

from sqlalchemy import select

from masm.retrieval.relation_expander import RelationExpander
from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryRelation
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _memory_id(database: Database, user_id: str, request_id: str) -> UUID:
    with database.session() as session:
        return session.execute(
            select(Memory.id).where(Memory.user_id == user_id, Memory.request_id == request_id)
        ).scalar_one()


def _seed(database, asset_store, settings, embeddings, user_id: str, text: str) -> UUID:
    request_id = _uid("r")
    AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings
    ).add(_request(request_id, user_id, text))
    return _memory_id(database, user_id, request_id)


def _poison_relation(database: Database, user_id: str, source_id: UUID, target_id: UUID) -> None:
    """绕过校验直接写入跨用户关系边，模拟数据库异常数据。"""
    with database.session() as session:
        session.add(
            MemoryRelation(
                user_id=user_id,
                source_type="memory",
                source_id=source_id,
                target_type="memory",
                target_id=target_id,
                relation_type="link",
                confidence=1.0,
            )
        )
        session.commit()


def test_related_filters_cross_user_edges_in_sql(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """异常跨用户关系边不得让另一用户的记忆出现在结果里。"""
    user_a, user_b = _uid("user-a"), _uid("user-b")
    memory_a = _seed(database, asset_store, settings, embeddings, user_a, "user a memory")
    memory_b = _seed(database, asset_store, settings, embeddings, user_b, "user b memory")
    _poison_relation(database, user_a, memory_a, memory_b)

    candidates = MemoryRepository(database).related(user_a, [memory_a], 10)

    assert memory_b not in {candidate.memory_id for candidate in candidates}
    assert all(candidate.user_id == user_a for candidate in candidates)


def test_relation_expander_never_returns_other_users_memories(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_a, user_b = _uid("user-a"), _uid("user-b")
    memory_a = _seed(database, asset_store, settings, embeddings, user_a, "user a memory")
    memory_b = _seed(database, asset_store, settings, embeddings, user_b, "user b memory")
    _poison_relation(database, user_a, memory_a, memory_b)
    expander = RelationExpander(MemoryRepository(database))
    seed = MemoryCandidate(
        memory_id=memory_a, user_id=user_a, content="user a memory", score=1.0
    )

    expanded = expander.expand(user_a, [seed], 10)

    assert all(candidate.user_id == user_a for candidate in expanded)
    assert memory_b not in {candidate.memory_id for candidate in expanded}


def test_conflict_peers_are_user_scoped(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """冲突补全同样只能返回同一用户的成员。"""
    user_a, user_b = _uid("user-a"), _uid("user-b")
    memory_a = _seed(database, asset_store, settings, embeddings, user_a, "conflict memory")
    memory_b = _seed(database, asset_store, settings, embeddings, user_b, "conflict memory")
    group = uuid4()
    with database.session() as session:
        for memory_id in (memory_a, memory_b):
            row = session.get(Memory, memory_id)
            assert row is not None
            row.conflict_group_id = group
        session.commit()

    peers = MemoryRepository(database).conflict_peers(user_a, [memory_a], 10)

    assert all(candidate.user_id == user_a for candidate in peers)
    assert memory_b not in {candidate.memory_id for candidate in peers}
