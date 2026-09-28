"""运行级删除的仓库层回归测试。

覆盖三处此前缺失的边界：
1. 清理悬空治理引用（duplicate_of / supersedes）时必须按 user_id 限制作用域；
2. 同一 ``request_id`` 被重新拥有后，删除意图必须能从 DONE 重置回 PENDING；
3. 「检查引用 -> 删除物理对象」必须在同一把对象 advisory 锁内完成，否则并发 Add 会丢对象。
"""

from uuid import UUID, uuid4

from sqlalchemy import select, text, update

from masm.storage.db import Database
from masm.storage.models import Asset, Memory, SourceMessage
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryBundle, MemoryDraft


def _uid(prefix: str) -> str:
    """生成每次运行都唯一的标识，避免跨测试污染。"""
    return f"{prefix}-{uuid4().hex[:12]}"


def _bundle(user_id: str, request_id: str, session_id: str, text: str) -> MemoryBundle:
    return MemoryBundle(
        session_id=session_id,
        request_id=request_id,
        memories=[MemoryDraft(summary=text, original_text=text, keywords=[])],
    )


def _add(repo: MemoryRepository, user_id: str, request_id: str, text: str, session: str) -> UUID:
    """写入一条属于该用户该运行的记忆，返回记忆主键。"""
    return repo.add_bundle(user_id, _bundle(user_id, request_id, session, text)).memory_ids[0]


def _point_at(repo: MemoryRepository, user_id: str, memory_id: UUID, target: UUID | None) -> None:
    """把一条记忆的治理引用指向（或解除指向）目标记忆。"""
    with repo._database.session() as session:
        session.execute(
            update(Memory)
            .where(Memory.id == memory_id, Memory.user_id == user_id)
            .values(duplicate_of=target, supersedes=target)
        )
        session.commit()


def _governance_refs(repo: MemoryRepository, user_id: str, memory_id: UUID) -> tuple:
    """重新从数据库读取治理引用，避免会话内置状态掩盖真实落库结果。"""
    with repo._database.session() as session:
        row = session.execute(
            select(Memory.duplicate_of, Memory.supersedes).where(
                Memory.id == memory_id, Memory.user_id == user_id
            )
        ).one()
    return (row.duplicate_of, row.supersedes)


def _locks_object_uri(database: Database, object_uri: str) -> bool:
    """用独立连接探测该对象地址是否已被其他连接持有 advisory 锁。

    探测能在 ``lock_timeout`` 内返回即说明锁未被持有（返回 True）；被阻塞说明锁正被
    持有（返回 False）。
    """
    with database.engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '2s'"))
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(hashtext(:uri)::bigint)"), {"uri": object_uri}
            )
        except Exception:
            connection.rollback()
            return False
        connection.execute(
            text("SELECT pg_advisory_unlock(hashtext(:uri)::bigint)"), {"uri": object_uri}
        )
        connection.rollback()
    return True


def _run_with_object(
    repo: MemoryRepository, user_id: str, request_id: str, object_uri: str
) -> UUID:
    """建立一个带图片引用的运行（记忆 + 源消息 + 资产行），返回记忆主键。"""
    memory_id = _add(repo, user_id, request_id, "memory with an object", "session-object")
    with repo._database.session() as session:
        session_id = session.execute(
            select(Memory.session_id).where(Memory.id == memory_id)
        ).scalar_one()
        session.add(
            SourceMessage(
                user_id=user_id,
                session_id=session_id,
                request_id=request_id,
                position=0,
                role="user",
                content=[{"type": "image_url", "image_url": {"url": object_uri}}],
            )
        )
        session.add(
            Asset(
                user_id=user_id,
                request_id=request_id,
                object_uri=object_uri,
                media_type="image/png",
                content_hash=uuid4().hex,
                decoded_size=1,
            )
        )
        session.commit()
    return memory_id


def test_delete_run_does_not_clear_other_users_governance_refs(database: Database) -> None:
    """A 的运行删除绝不能把 B 记忆上的 duplicate_of / supersedes 清空。"""
    repo = MemoryRepository(database)
    user_a, user_b = _uid("user-a"), _uid("user-b")
    run_a, run_b = _uid("run-a"), _uid("run-b")

    victim = _add(repo, user_a, run_a, "alpha memory", "session-a")
    other_user_memory = _add(repo, user_b, run_b, "beta memory", "session-b")
    _point_at(repo, user_b, other_user_memory, victim)

    repo.delete_run(user_a, run_a)

    assert _governance_refs(repo, user_b, other_user_memory) == (victim, victim)


def test_delete_run_clears_same_user_dangling_refs(database: Database) -> None:
    """同一用户内部的悬空引用仍必须被清理（修复不能过头）。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    run_old, run_new = _uid("run"), _uid("run")

    victim = _add(repo, user, run_old, "old memory", "session-1")
    survivor = _add(repo, user, run_new, "new memory", "session-2")
    _point_at(repo, user, survivor, victim)

    repo.delete_run(user, run_old)

    assert _governance_refs(repo, user, survivor) == (None, None)


def test_deletion_intent_is_reset_when_request_id_is_reused(database: Database) -> None:
    """request_id 被重新拥有后，已 DONE 的删除意图必须回到 PENDING。"""
    repo = MemoryRepository(database)
    user_id = _uid("user")
    run_id = _uid("run")
    first_uri, second_uri = f"{user_id}/{uuid4().hex}.png", f"{user_id}/{uuid4().hex}.png"

    repo.register_deletion_intents(user_id, run_id, [first_uri])
    assert repo.pending_deletion_uris(user_id, run_id) == [first_uri]

    repo.mark_deletion_intent(user_id, run_id, first_uri, done=True)
    assert repo.pending_deletion_uris(user_id, run_id) == []

    # 同一 request_id 被新的运行重新拥有，并再次登记同一对象地址。
    repo.register_deletion_intents(user_id, run_id, [first_uri, second_uri])

    assert repo.pending_deletion_uris(user_id, run_id) == sorted([first_uri, second_uri])


def test_delete_run_holds_object_lock_while_deleting(database: Database, monkeypatch) -> None:
    """物理删除必须发生在持有该对象 advisory 锁的临界区内。

    在临界区回调执行的瞬间用独立连接探测同一把锁：若实现已持有锁，探测必然被阻塞
    （说明「检查引用 -> 删除物理对象」受保护）；若实现未加锁，探测会立即成功。
    """
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)

    probes: list[bool] = []
    handed_to_delete: list[str] = []

    def _probe(self: MemoryRepository, object_uri: str, delete_object: object) -> tuple[str, str]:
        handed_to_delete.append(object_uri)
        probes.append(_locks_object_uri(database, object_uri))
        return ("deleted", "")

    monkeypatch.setattr(MemoryRepository, "_delete_object_locked", _probe, raising=True)

    report = repo.delete_run(user_id, run_id, delete_object=lambda uri: True)

    assert handed_to_delete == [object_uri]
    assert probes == [False], "临界区内不应有其他连接能取得同一把对象锁"
    assert report.object_uris == (object_uri,)
    assert report.objects_deleted == 1


def test_delete_run_releases_lock_after_finishing(database: Database) -> None:
    """删除结束后锁必须释放。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    calls: list[str] = []

    def _store_delete(uri: str) -> bool:
        calls.append(uri)
        return True

    report = repo.delete_run(user_id, run_id, delete_object=_store_delete)

    assert calls == [object_uri]
    assert report.object_uris == (object_uri,)
    assert _locks_object_uri(database, object_uri) is True


def test_delete_run_keeps_shared_objects(database: Database) -> None:
    """仍被其他运行引用的对象必须保留，且不进入待删清单。"""
    repo = MemoryRepository(database)
    user_id = _uid("user")
    run_one, run_two = _uid("run"), _uid("run")
    shared_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_one, shared_uri)
    _run_with_object(repo, user_id, run_two, shared_uri)
    deleted: list[str] = []

    def _store_delete(uri: str) -> bool:
        deleted.append(uri)
        return True

    report = repo.delete_run(user_id, run_one, delete_object=_store_delete)

    assert deleted == []
    assert report.object_uris == ()
    assert report.shared_object_uris == (shared_uri,)


def test_delete_run_reports_missing_objects(database: Database) -> None:
    """物理对象已不存在时必须可观察，而不是伪装成删除成功。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    calls: list[str] = []

    def _store_delete(uri: str) -> bool:
        calls.append(uri)
        return False

    report = repo.delete_run(user_id, run_id, delete_object=_store_delete)

    assert calls == [object_uri]
    assert report.objects_missing == 1
    assert report.object_failures == ()
    assert repo.pending_deletion_uris(user_id, run_id) == []


def test_delete_run_keeps_objects_referenced_by_null_request_assets(database: Database) -> None:
    """迁移前遗留（request_id 为 NULL）的资产行仍构成引用，对象不得被删除。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    with repo._database.session() as session:
        # 模拟迁移未回填成功的历史资产行（0003 之前的写入形态）。
        session.execute(
            update(Asset)
            .where(Asset.user_id == user_id, Asset.object_uri == object_uri)
            .values(request_id=None)
        )
        session.commit()

    assert repo.deletable_object_uris(user_id, run_id) == []
    report = repo.delete_run(user_id, run_id, delete_object=lambda uri: True)

    assert report.object_uris == ()
    assert report.shared_object_uris == (object_uri,)


def test_delete_run_without_callback_leaves_intents_pending(database: Database) -> None:
    """未提供删除回调时不得触动物理对象，只报告待删清单。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)

    report = repo.delete_run(user_id, run_id)

    assert report.object_uris == (object_uri,)
    assert report.objects_deleted == 0
    assert report.objects_missing == 0
    assert report.object_failures == ()


def test_delete_run_keeps_intent_pending_when_delete_fails(database: Database) -> None:
    """物理删除抛异常时必须在结果中暴露失败对象及其错误类型，供调用方保留重试。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)

    def _explode(uri: str) -> bool:
        raise OSError("locked object")

    report = repo.delete_run(user_id, run_id, delete_object=_explode)

    assert report.object_uris == (object_uri,)
    assert report.objects_deleted == 0
    assert report.objects_missing == 0
    assert report.object_failures == ((object_uri, "OSError"),)
