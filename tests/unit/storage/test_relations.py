"""关系边同用户验证单元测试。"""

from uuid import UUID, uuid4

import pytest

from masm.storage.db import Database
from masm.storage.models import MemoryRelation
from masm.storage.repositories import MemoryRepository, RelationValidationError
from masm.storage.types import MemoryBundle, MemoryDraft


def _uid(prefix: str) -> str:
    """生成每次运行都唯一的用户标识。"""
    return f"{prefix}-{uuid4().hex[:12]}"


def _memory_id(repo: MemoryRepository, user_id: str, text: str) -> UUID:
    """写入一条记忆并返回其 memory_id。"""
    bundle = MemoryBundle(
        session_id=f"session-{user_id}",
        request_id=f"request-{uuid4().hex}",
        memories=[MemoryDraft(summary=text, original_text=text)],
    )
    return repo.add_bundle(user_id, bundle).memory_ids[0]


@pytest.fixture(scope="module")
def database(database_url: str) -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(database_url)


def test_add_relation_same_user(database: Database) -> None:
    """同用户两端可以创建关系。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    source = _memory_id(repo, user, "source")
    target = _memory_id(repo, user, "target")

    relation_id = repo.add_relation(
        user,
        source_type="memory",
        source_id=source,
        target_type="memory",
        target_id=target,
        relation_type="related",
    )
    assert isinstance(relation_id, UUID)


def test_add_relation_rejects_cross_user(database: Database) -> None:
    """跨用户两端必须被拒绝。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    source = _memory_id(repo, user_a, "source")
    target = _memory_id(repo, user_b, "target")

    with pytest.raises(RelationValidationError):
        repo.add_relation(
            user_a,
            source_type="memory",
            source_id=source,
            target_type="memory",
            target_id=target,
            relation_type="related",
        )


def test_add_relation_rejects_unknown_node_type(database: Database) -> None:
    """不允许的节点类型必须被拒绝。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    source = _memory_id(repo, user, "source")

    with pytest.raises(RelationValidationError):
        repo.add_relation(
            user,
            source_type="banana",
            source_id=source,
            target_type="memory",
            target_id=source,
            relation_type="related",
        )


def test_add_relation_rejects_missing_node(database: Database) -> None:
    """不存在的节点必须被拒绝。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    source = _memory_id(repo, user, "source")

    with pytest.raises(RelationValidationError):
        repo.add_relation(
            user,
            source_type="memory",
            source_id=source,
            target_type="memory",
            target_id=uuid4(),
            relation_type="related",
        )


def test_related_only_returns_target_user(database: Database) -> None:
    """related() 只返回目标用户记录。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    a1 = _memory_id(repo, user_a, "a1")
    a2 = _memory_id(repo, user_a, "a2")
    b1 = _memory_id(repo, user_b, "b1")
    b2 = _memory_id(repo, user_b, "b2")

    repo.add_relation(
        user_a,
        source_type="memory",
        source_id=a1,
        target_type="memory",
        target_id=a2,
        relation_type="related",
    )
    repo.add_relation(
        user_b,
        source_type="memory",
        source_id=b1,
        target_type="memory",
        target_id=b2,
        relation_type="related",
    )

    results = repo.related(user_a, [a1], limit=10)
    assert all(c.user_id == user_a for c in results)
    assert {c.memory_id for c in results} == {a2}


def test_anomalous_cross_user_relation_no_leak(database: Database) -> None:
    """异常的跨用户关系数据也不能造成泄漏。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    a = _memory_id(repo, user_a, "a")
    b = _memory_id(repo, user_b, "b")

    # 绕过校验，直接插入异常的跨用户关系边。
    with database.session() as session:
        session.add(
            MemoryRelation(
                user_id=user_a,
                source_type="memory",
                source_id=a,
                target_type="memory",
                target_id=b,
                relation_type="related",
            )
        )
        session.commit()

    results = repo.related(user_a, [a], limit=10)
    assert all(c.user_id == user_a for c in results)
    assert b not in {c.memory_id for c in results}
