"""运行级删除的仓库层回归测试。

覆盖此前缺失的边界：
1. 清理悬空治理引用（duplicate_of / supersedes）时必须按 user_id 限制作用域；
2. 同一 ``request_id`` 被重新拥有后，删除意图必须能从 DONE 重置回 PENDING；
3. 「引用检查 -> 物理删除」必须在同一把对象锁内完成，否则并发 Add 会丢对象；
4. 物理删除必须跨事务重试：第一次失败后 SourceMessage 已删除，第二次只能靠持久化意图；
5. 数据库提交失败时绝不能提前删除物理对象（删除不在 commit 之前发生）。
"""

from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update

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
    repo: MemoryRepository,
    user_id: str,
    request_id: str,
    object_uri: str,
    *,
    asset_request_id: str | None | object = ...,
) -> UUID:
    """建立一个带图片引用的运行（记忆 + 源消息 + 资产行），返回记忆主键。

    ``asset_request_id`` 用于模拟迁移前遗留的资产行（显式传 None）。
    """
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
                request_id=request_id if asset_request_id is ... else asset_request_id,
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


def test_delete_run_registers_pending_intent_without_touching_storage(database: Database) -> None:
    """数据库阶段只登记 PENDING 意图：绝不触碰对象存储，也不产生删除结果。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)

    deleted = repo.delete_run(user_id, run_id)

    assert deleted.object_uris == (object_uri,)
    assert repo.pending_deletion_uris(user_id, run_id) == [object_uri]


def test_retry_uses_persisted_intents_after_source_messages_are_gone(database: Database) -> None:
    """第一次删除失败后 SourceMessage 已删除，重试必须仍能靠持久化意图找到 URI。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)

    repo.delete_run(user_id, run_id)
    with repo._database.session() as session:
        remaining_messages = int(
            session.execute(
                select(func.count())
                .select_from(SourceMessage)
                .where(
                    SourceMessage.user_id == user_id,
                    SourceMessage.request_id == run_id,
                )
            ).scalar_one()
        )
    assert remaining_messages == 0

    def _explode(uri: str) -> bool:
        raise OSError("storage unavailable")

    first = repo.retry_pending_object_deletions(user_id, run_id, _explode)
    assert first.deleted == 0
    assert first.failed == ((object_uri, "OSError"),)
    assert repo.pending_deletion_uris(user_id, run_id) == [object_uri]

    # 存储恢复后重试：不再有 SourceMessage 可依赖。
    calls: list[str] = []

    def _recover(uri: str) -> bool:
        calls.append(uri)
        return True

    second = repo.retry_pending_object_deletions(user_id, run_id, _recover)

    assert calls == [object_uri]
    assert second.deleted == 1
    assert repo.pending_deletion_uris(user_id, run_id) == []


def test_retry_completes_inside_the_object_lock(database: Database, monkeypatch) -> None:
    """物理删除必须发生在持有该对象 advisory 锁的临界区内。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    repo.delete_run(user_id, run_id)

    probes: list[bool] = []
    handed_to_store: list[str] = []

    def _probe(uri: str) -> bool:
        handed_to_store.append(uri)
        probes.append(_locks_object_uri(database, uri))
        return True

    repo.retry_pending_object_deletions(user_id, run_id, _probe)

    assert handed_to_store == [object_uri]
    assert probes == [False], "临界区内不应有其他连接能取得同一把对象锁"


def test_retry_releases_lock_after_finishing(database: Database) -> None:
    """重试结束后锁必须释放。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    repo.delete_run(user_id, run_id)

    repo.retry_pending_object_deletions(user_id, run_id, lambda uri: True)

    assert _locks_object_uri(database, object_uri) is True


def test_retry_keeps_shared_objects(database: Database) -> None:
    """仍被其他运行引用的对象必须保留，且不进入待删清单。"""
    repo = MemoryRepository(database)
    user_id = _uid("user")
    run_one, run_two = _uid("run"), _uid("run")
    shared_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_one, shared_uri)
    _run_with_object(repo, user_id, run_two, shared_uri)

    deleted = repo.delete_run(user_id, run_one)
    outcome = repo.retry_pending_object_deletions(user_id, run_one, lambda uri: True)

    assert deleted.object_uris == ()
    assert deleted.shared_object_uris == (shared_uri,)
    assert outcome.deleted == 0
    assert repo.pending_deletion_uris(user_id, run_one) == []


def test_retry_keeps_objects_referenced_by_null_request_assets(database: Database) -> None:
    """迁移前遗留（request_id 为 NULL）的资产行仍构成引用，对象不得被删除。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri, asset_request_id=None)

    deleted = repo.delete_run(user_id, run_id)
    calls: list[str] = []
    outcome = repo.retry_pending_object_deletions(
        user_id, run_id, lambda uri: calls.append(uri) or True
    )

    assert deleted.object_uris == ()
    assert deleted.shared_object_uris == (object_uri,)
    assert calls == []
    assert outcome.deleted == 0


def test_retry_keeps_objects_referenced_by_other_runs_messages(database: Database) -> None:
    """只有一行资产行、但另一个运行的原始消息仍引用时，对象不得被删除。"""
    repo = MemoryRepository(database)
    user_id = _uid("user")
    run_one, run_two = _uid("run"), _uid("run")
    shared_uri = f"{user_id}/{uuid4().hex}.png"
    # run_one 拥有唯一一行资产记录；run_two 只在原始消息里引用同一对象。
    _run_with_object(repo, user_id, run_one, shared_uri)
    _run_with_object(repo, user_id, run_two, shared_uri)
    with repo._database.session() as session:
        session.execute(
            update(Asset)
            .where(Asset.user_id == user_id, Asset.object_uri == shared_uri)
            .values(request_id=None)
        )
        session.execute(
            update(SourceMessage)
            .where(SourceMessage.request_id == run_two)
            .values(request_id=run_one)
        )
        session.commit()

    deleted = repo.delete_run(user_id, run_one)
    calls: list[str] = []
    repo.retry_pending_object_deletions(user_id, run_one, lambda uri: calls.append(uri) or True)

    assert calls == []
    assert deleted.object_uris == ()
    assert deleted.shared_object_uris == (shared_uri,)


def test_retry_reports_missing_objects_as_done(database: Database) -> None:
    """物理对象已不存在时必须可观察，并确认删除（不再重试）。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    repo.delete_run(user_id, run_id)

    outcome = repo.retry_pending_object_deletions(user_id, run_id, lambda uri: False)

    assert outcome.missing == 1
    assert outcome.deleted == 0
    assert repo.pending_deletion_uris(user_id, run_id) == []


def test_retry_keeps_intent_pending_when_delete_fails(database: Database) -> None:
    """物理删除抛异常时意图必须保持 PENDING 并累加尝试次数。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    repo.delete_run(user_id, run_id)

    def _explode(uri: str) -> bool:
        raise OSError("locked object")

    outcome = repo.retry_pending_object_deletions(user_id, run_id, _explode)

    assert outcome.failed == ((object_uri, "OSError"),)
    assert repo.pending_deletion_uris(user_id, run_id) == [object_uri]
    intents = repo.pending_deletion_intents(user_id, run_id)
    assert intents[0].attempts == 1
    assert intents[0].last_error == "OSError"


def test_commit_failure_never_deletes_physical_objects(database: Database) -> None:
    """数据库提交失败（回滚）时，物理对象绝不能被提前删除。"""
    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    memory_id = _run_with_object(repo, user_id, run_id, object_uri)

    def _fail_commit(session) -> None:
        raise RuntimeError("commit failed")

    failing = MemoryRepository(database, commit=_fail_commit)
    try:
        failing.delete_run(user_id, run_id)
    except RuntimeError:
        pass

    # 行删除与删除意图必须一起回滚：记忆仍在，也没有任何 PENDING 意图。
    with database.session() as session:
        alive = session.execute(
            select(Memory.id).where(Memory.id == memory_id, Memory.user_id == user_id)
        ).scalar_one_or_none()
    assert alive == memory_id
    assert repo.pending_deletion_uris(user_id, run_id) == []
    # 物理对象从未被触碰（本阶段本来也不该触碰）。
    assert repo.deletable_object_uris(user_id, run_id) == [object_uri]


def test_retry_waits_for_lock_then_succeeds(database: Database) -> None:
    """lock_timeout 必须在加锁之前设置：短暂持锁者释放后，重试应正常完成。"""
    import threading
    import time

    repo = MemoryRepository(database)
    user_id, run_id = _uid("user"), _uid("run")
    object_uri = f"{user_id}/{uuid4().hex}.png"
    _run_with_object(repo, user_id, run_id, object_uri)
    repo.delete_run(user_id, run_id)

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold() -> None:
        with database.engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(hashtext(:uri)::bigint)"), {"uri": object_uri}
            )
            holder_ready.set()
            release_holder.wait(6)
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:uri)::bigint)"), {"uri": object_uri}
            )
            connection.rollback()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert holder_ready.wait(5)

    result: dict = {}

    def _worker() -> None:
        result["outcome"] = repo.retry_pending_object_deletions(user_id, run_id, lambda uri: True)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    time.sleep(1.0)
    release_holder.set()
    worker.join(10)

    assert not worker.is_alive()
    assert result["outcome"].deleted == 1
    assert repo.pending_deletion_uris(user_id, run_id) == []
    holder.join(5)
