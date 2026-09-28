"""评测运行删除集成测试：数据库 + 对象存储，幂等且作用域明确。"""

import base64
import io
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Asset, Memory, SessionRecord, SourceMessage, User
from masm.storage.repositories import MemoryRepository, try_lock_run_lifecycle


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _data_url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


def _add(service: AddService, request_id: str, user_id: str, session_id: str = "session-1") -> None:
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a memorable photo"},
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                ],
            }
        ],
    )
    assert service.add(request).success is True


def _count(database: Database, model: type, user_id: str) -> int:
    with database.session() as session:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )


def _objects(store: AssetStore) -> list[Path]:
    """已发布（正式）对象文件；暂存目录内的私有文件不计入。"""
    if not store.base_dir.exists():
        return []
    staging_root = store.base_dir / "staging"
    return [
        path
        for path in store.base_dir.rglob("*")
        if path.is_file() and staging_root not in path.parents
    ]


def _service(database: Database, store: AssetStore, settings, embeddings) -> DeletionService:
    return DeletionService(MemoryRepository(database), store)


def test_delete_run_removes_database_rows_and_objects(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    run_id = _uid("r")
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    _add(service, run_id, user_id)
    assert _count(database, Memory, user_id) == 1
    assert _objects(asset_store)

    report = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert report.complete is True
    assert report.memories_deleted == 1
    assert report.assets_deleted == 1
    assert report.objects_deleted >= 1
    assert _count(database, Memory, user_id) == 0
    assert _count(database, Asset, user_id) == 0
    assert _count(database, SourceMessage, user_id) == 0
    assert _objects(asset_store) == []


def test_delete_run_is_idempotent(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    run_id = _uid("r")
    AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings).add(
        AddRequest(
            request_id=run_id,
            user_id=user_id,
            session_id="session-1",
            messages=[{"role": "user", "content": "deletable memory"}],
        )
    )
    deleter = _service(database, asset_store, settings, embeddings)

    first = deleter.delete_run(run_id, user_id=user_id)
    second = deleter.delete_run(run_id, user_id=user_id)

    assert first.complete is True
    assert second.complete is True
    assert second.memories_deleted == 0
    assert second.objects_deleted == 0


def test_delete_run_is_scoped_to_user_and_run(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_a, user_b = _uid("user-a"), _uid("user-b")
    run_a1, run_a2, run_b1 = _uid("r"), _uid("r"), _uid("r")
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    _add(service, run_a1, user_a, "session-a1")
    _add(service, run_a2, user_a, "session-a2")
    _add(service, run_b1, user_b, "session-b1")

    _service(database, asset_store, settings, embeddings).delete_run(run_a1, user_id=user_a)

    assert _count(database, Memory, user_a) == 1
    assert _count(database, Memory, user_b) == 1
    with database.session() as session:
        remaining = (
            session.execute(select(Memory.request_id).where(Memory.user_id == user_a))
            .scalars()
            .all()
        )
    assert remaining == [run_a2]


def test_delete_run_reports_partial_object_failure(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """对象删除失败必须可观察，不能伪装为完全成功。"""
    user_id = _uid("u")
    run_id = _uid("r")
    _add(
        AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
        run_id,
        user_id,
    )
    deleter = _service(database, asset_store, settings, embeddings)

    def _explode(self, relative: str) -> None:
        raise OSError("locked object")

    monkeypatch.setattr(AssetStore, "delete_object", _explode, raising=False)

    report = deleter.delete_run(run_id, user_id=user_id)

    assert report.complete is False
    assert report.failed_object_uris


def test_lock_contention_leaves_object_pending_and_file_intact(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """对象锁被长时间持有时：删除请求快速失败，物理文件完好，意图保持 PENDING。"""
    user_id = _uid("u")
    run_id = _uid("r")
    _add(
        AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
        run_id,
        user_id,
    )
    repo = MemoryRepository(database)
    files_before = _objects(asset_store)
    uris = repo.deletable_object_uris(user_id, run_id)
    assert files_before and uris

    holder = _hold_object_locks(database, uris, hold_seconds=4.0)
    holder.ready.wait(5)

    # 打桩把等待上限压到 1 秒：删除必须在持锁者释放之前快速失败。
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("masm.storage.repositories._LOCK_TIMEOUT_SECONDS", 1)
        with pytest.raises(Exception) as failure:
            _service(database, asset_store, settings, embeddings).delete_run(
                run_id, user_id=user_id
            )

    assert "lock" in str(failure.value).lower() or "timeout" in str(failure.value).lower()
    assert _objects(asset_store) == files_before, "锁竞争时不得提前删除物理对象"
    assert repo.pending_deletion_uris(user_id, run_id), "失败对象必须保持 PENDING 以便重试"

    holder.release.wait(10)

    # 锁释放后重试即可收敛。
    report = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )
    assert report.complete is True
    assert _objects(asset_store) == []


def _hold_object_locks(database: Database, object_uris: list[str], *, hold_seconds: float):
    """在独立连接上持有这些对象地址的 advisory 锁，并在指定时间后释放。"""
    import threading
    import time

    from sqlalchemy import text

    ready = threading.Event()
    release = threading.Event()
    connection = database.engine.connect()

    def _hold() -> None:
        try:
            for object_uri in sorted(object_uris):
                connection.execute(
                    text("SELECT pg_advisory_lock(hashtext(:uri)::bigint)"), {"uri": object_uri}
                )
            connection.commit()
            ready.set()
            deadline = time.monotonic() + hold_seconds
            while time.monotonic() < deadline and not release.is_set():
                time.sleep(0.05)
            connection.execute(text("SELECT pg_advisory_unlock_all()"))
            connection.commit()
        finally:
            release.set()

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    return _LockHandle(thread=thread, ready=ready, release=release)


@dataclass
class _LockHandle:
    """测试用锁持有者句柄。"""

    thread: object
    ready: object
    release: object


def test_deletion_retries_from_persisted_intent_after_failure(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """第一次对象删除失败后，第二次调用只能靠持久化删除意图恢复并真正删除文件。

    第一次失败时 SourceMessage 已随运行删除，因此这里同时验证「重试不依赖原始消息」。
    """
    user_id = _uid("u")
    run_id = _uid("r")
    _add(
        AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
        run_id,
        user_id,
    )
    repo = MemoryRepository(database)
    files_before = _objects(asset_store)
    assert files_before

    original = AssetStore.delete_object

    def _explode(self, relative: str) -> bool:
        raise OSError("storage unavailable")

    monkeypatch.setattr(AssetStore, "delete_object", _explode, raising=False)
    first = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert first.complete is False
    assert repo.pending_deletion_uris(user_id, run_id)
    assert _objects(asset_store) == files_before
    # 原始消息已经删除：重试不能再依赖它推导 URI。
    assert _count(database, SourceMessage, user_id) == 0

    monkeypatch.setattr(AssetStore, "delete_object", original, raising=False)
    second = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert second.complete is True
    assert second.objects_deleted == len(files_before)
    assert repo.pending_deletion_uris(user_id, run_id) == []
    assert _objects(asset_store) == []


def test_delete_run_clears_intent_and_marks_done(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """成功删除对象后删除意图必须落为 DONE，重复调用不再报告待清理。"""
    user_id = _uid("u")
    run_id = _uid("r")
    _add(
        AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
        run_id,
        user_id,
    )
    repo = MemoryRepository(database)
    deleter = _service(database, asset_store, settings, embeddings)

    first = deleter.delete_run(run_id, user_id=user_id)

    assert first.complete is True
    assert first.objects_deleted >= 1
    assert repo.pending_deletion_uris(user_id, run_id) == []


def test_reused_request_id_reactivates_deletion_intent(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """同一 request_id 被重新拥有后，新的对象必须重新进入 PENDING 并真正被删除。"""
    user_id = _uid("u")
    run_id = _uid("r")
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    repo = MemoryRepository(database)
    deleter = _service(database, asset_store, settings, embeddings)

    _add(add, run_id, user_id)
    assert deleter.delete_run(run_id, user_id=user_id).complete is True
    assert repo.pending_deletion_uris(user_id, run_id) == []

    # 重跑同一 request_id（评测重试），再次登记同一对象地址。
    _add(add, run_id, user_id)
    assert _objects(asset_store)

    report = deleter.delete_run(run_id, user_id=user_id)

    assert report.complete is True
    assert report.objects_deleted >= 1
    assert _objects(asset_store) == []


def test_delete_run_rejects_path_traversal(
    database: Database, asset_store: AssetStore, settings, embeddings, tmp_path
) -> None:
    """对象路径必须安全解析，禁止越界删除。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    store = AssetStore(tmp_path / "assets")
    store.base_dir.mkdir(parents=True, exist_ok=True)
    poisoned_user = _uid("user-poison")
    poisoned_run = _uid("r")
    with database.session() as session:
        session.add(User(user_id=poisoned_user))
        session.flush()
        session.add(
            Asset(
                user_id=poisoned_user,
                request_id=poisoned_run,
                object_uri="../../outside.txt",
                media_type="image/png",
                content_hash="deadbeef",
                decoded_size=1,
            )
        )
        session.commit()

    with database.session() as session:
        session.add(SessionRecord(user_id=poisoned_user, session_id="poison-session"))
        session.flush()
        session_id = session.execute(
            select(SessionRecord.id).where(SessionRecord.user_id == poisoned_user)
        ).scalar_one()
        session.add(
            SourceMessage(
                user_id=poisoned_user,
                session_id=session_id,
                request_id=poisoned_run,
                position=0,
                role="user",
                content=[{"type": "image_url", "image_url": {"url": "../../outside.txt"}}],
            )
        )
        session.commit()

    report = _service(database, asset_store, settings, embeddings).delete_run(
        poisoned_run, user_id=poisoned_user
    )

    # 关键不变量：越界路径绝不被删除（无论它被判定为可删除还是被保护）。
    assert outside.exists()
    assert report.objects_deleted == 0


def test_concurrent_add_and_delete_never_loses_the_new_object(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """真正并发启动 Add 与 Delete：新运行刚提交的对象绝不能被删除。

    多轮端到端竞态（不是「探测锁是否被持有」）：每轮让 Add 线程与 Delete 线程在同一个
    ``threading.Barrier`` 上同时放行，两者操作同一个内容寻址对象地址；每轮结束后断言
    「数据库里新运行引用的每个对象都仍然存在」，最后以整体不变量收尾。
    """
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    rounds = 6

    for round_index in range(rounds):
        user_id = _uid("u")
        old_run, new_run = _uid("r-old"), _uid("r-new")
        _add(add, old_run, user_id, f"session-old-{round_index}")

        writer, deleter, errors = _race_add_and_delete(
            database,
            asset_store,
            settings,
            embeddings,
            user_id=user_id,
            old_run=old_run,
            new_run=new_run,
        )
        writer.join(30)
        deleter.join(30)

        assert not writer.is_alive() and not deleter.is_alive(), (
            f"第 {round_index} 轮并发 Add/Delete 未在超时内完成"
        )
        assert errors == [], f"第 {round_index} 轮并发执行出现异常: {errors!r}"

        new_assets = _asset_uris(database, user_id, new_run)
        assert new_assets, f"第 {round_index} 轮新运行的资产行应当存在"
        present = {
            str(path.relative_to(asset_store.base_dir)).replace("\\", "/")
            for path in _objects(asset_store)
        }
        for object_uri in new_assets:
            assert object_uri in present, (
                f"第 {round_index} 轮并发删除丢掉了新运行的对象: {object_uri}"
            )


def _asset_uris(database: Database, user_id: str, request_id: str) -> list[str]:
    with database.session() as session:
        return list(
            session.execute(
                select(Asset.object_uri).where(
                    Asset.user_id == user_id, Asset.request_id == request_id
                )
            ).scalars()
        )


def _race_add_and_delete(
    database: Database,
    asset_store: AssetStore,
    settings,
    embeddings,
    *,
    user_id: str,
    old_run: str,
    new_run: str,
):
    """在同一个 Barrier 上同时放行 Add 与 Delete，返回线程与共享错误列表。"""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)

    def _writer() -> None:
        try:
            barrier.wait(15)
            _add(add, new_run, user_id, f"session-new-{new_run}")
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            errors.append(exc)

    def _deleter() -> None:
        try:
            barrier.wait(15)
            _service(database, asset_store, settings, embeddings).delete_run(
                old_run, user_id=user_id
            )
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            errors.append(exc)

    writer = threading.Thread(target=_writer, daemon=True)
    deleter = threading.Thread(target=_deleter, daemon=True)
    writer.start()
    deleter.start()
    return writer, deleter, errors


def _staging_files(store: AssetStore) -> list[Path]:
    """暂存目录里的私有文件（发布失败后可能残留）。"""
    staging_root = store.base_dir / "staging"
    if not staging_root.exists():
        return []
    return [path for path in staging_root.rglob("*") if path.is_file()]


def _staging_dirs(store: AssetStore) -> list[Path]:
    """暂存目录本身（存在即为隐私数据残留）。"""
    staging_root = store.base_dir / "staging"
    if not staging_root.exists():
        return []
    return [path for path in staging_root.rglob("*") if path.is_dir()]


def test_add_finalize_then_publish_is_serialized_with_delete(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """确定性复现「Add finalize 后暂停 -> Delete -> Add publish」竞态。

    顺序完全由事件控制，不依赖随机线程交错：
    1. Add 完成数据库 finalize，在 publish 之前阻塞；
    2. 此时 Delete 无法进入（同一 user_id + request_id 的生命周期锁被 Add 持有）；
    3. 放行 Add，它发布对象并返回；Delete 随后完成，清理数据库行、正式对象与暂存目录；
    4. 最终不存在任何孤儿对象，暂存目录也不存在。
    """
    from masm.storage.assets import _publish_object

    user_id = _uid("u")
    run_id = _uid("r")
    allow_publish = threading.Event()
    publish_blocked = threading.Event()

    def _blocking_publish(staged: Path, final: Path) -> None:
        publish_blocked.set()
        assert allow_publish.wait(30), "测试未能放行 publish"
        _publish_object(staged, final)

    monkeypatch.setattr("masm.storage.assets._publish_object", _blocking_publish)

    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    errors: list[BaseException] = []

    def _run_add() -> None:
        try:
            _add(add, run_id, user_id)
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            errors.append(exc)

    add_thread = threading.Thread(target=_run_add, daemon=True)
    add_thread.start()
    assert publish_blocked.wait(30), "Add 未到达 publish 阶段"

    # 生命周期锁此刻被 Add 持有：Delete 与任何并发 Add 都无法进入。
    assert try_lock_run_lifecycle(database.engine, user_id, run_id, lock_timeout_seconds=1) is False

    report_holder: dict = {}

    def _run_delete() -> None:
        try:
            report_holder["report"] = _service(
                database, asset_store, settings, embeddings
            ).delete_run(run_id, user_id=user_id)
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            errors.append(exc)

    delete_thread = threading.Thread(target=_run_delete, daemon=True)
    delete_thread.start()
    # Delete 必须被 Add 挡住，而不是抢先把行删完、再让 Add 发布孤儿对象。
    time.sleep(1.0)
    assert delete_thread.is_alive(), "Delete 未等待 Add 的 finalize->publish 完成"

    allow_publish.set()
    add_thread.join(30)
    delete_thread.join(30)

    assert errors == [], f"并发执行出现异常: {errors!r}"
    assert not add_thread.is_alive() and not delete_thread.is_alive()

    report = report_holder["report"]
    assert report.complete is True
    # 最终不变量：数据库行、正式对象、暂存文件与暂存目录都不存在。
    assert _count(database, Memory, user_id) == 0
    assert _asset_uris(database, user_id, run_id) == []
    assert _objects(asset_store) == []
    assert _staging_files(asset_store) == []
    assert _staging_dirs(asset_store) == []
    assert try_lock_run_lifecycle(database.engine, user_id, run_id) is True


def test_add_holds_lifecycle_lock_before_writing_staging(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """图片暂存写入也必须位于生命周期锁内，防止 Delete 先完成后留下暂存孤儿。"""
    user_id = _uid("u")
    run_id = _uid("r")
    put_started = threading.Event()
    allow_put = threading.Event()
    original_put = AssetStore.put

    def _blocking_put(self, user_id_arg, request_id_arg, owner_token, image):
        put_started.set()
        assert allow_put.wait(30), "测试未能放行 put"
        return original_put(self, user_id_arg, request_id_arg, owner_token, image)

    monkeypatch.setattr(AssetStore, "put", _blocking_put)
    errors: list[BaseException] = []

    def _run_add() -> None:
        try:
            _add(
                AddService(
                    MemoryRepository(database), asset_store, settings, embeddings=embeddings
                ),
                run_id,
                user_id,
            )
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            errors.append(exc)

    add_thread = threading.Thread(target=_run_add, daemon=True)
    add_thread.start()
    assert put_started.wait(30), "Add 未到达暂存写入阶段"

    # 如果 put 已经受到同一把生命周期锁保护，Delete 此时就不可能完成。
    delete_errors: list[BaseException] = []

    def _run_delete() -> None:
        try:
            _service(database, asset_store, settings, embeddings).delete_run(
                run_id, user_id=user_id
            )
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            delete_errors.append(exc)

    delete_thread = threading.Thread(target=_run_delete, daemon=True)
    delete_thread.start()
    delete_thread.join(2)
    delete_was_blocked = delete_thread.is_alive()
    allow_put.set()
    add_thread.join(30)
    delete_thread.join(30)

    assert errors == [], f"Add 执行出现异常: {errors!r}"
    assert delete_errors == [], f"Delete 执行出现异常: {delete_errors!r}"
    assert not add_thread.is_alive() and not delete_thread.is_alive()
    assert delete_was_blocked is True


def test_replay_rechecks_commit_under_lifecycle_lock_before_publish(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """回放不得用锁外旧账本在删除之后重新发布已撤销运行的暂存对象。"""
    from masm.storage.assets import _publish_object

    user_id = _uid("u")
    run_id = _uid("r")
    request = AddRequest(
        request_id=run_id,
        user_id=user_id,
        session_id="session-replay-race",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "replay race"},
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                ],
            }
        ],
    )

    def _failing_publish(staged: Path, final: Path) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr("masm.storage.assets._publish_object", _failing_publish)
    with pytest.raises(OSError):
        AddService(
            MemoryRepository(database), asset_store, settings, embeddings=embeddings
        ).add(request)
    assert _staging_files(asset_store)

    monkeypatch.setattr("masm.storage.assets._publish_object", _publish_object)
    retry_repo = MemoryRepository(database)
    retry = AddService(retry_repo, asset_store, settings, embeddings=embeddings)
    stale_ledger_read = threading.Event()
    allow_replay_lock = threading.Event()
    original_replay = retry._replay

    def _blocking_replay(request_arg: AddRequest):
        # add() 已在进入 _replay 前读到 COMMITTED；在取得生命周期锁之前暂停。
        stale_ledger_read.set()
        assert allow_replay_lock.wait(30), "测试未能放行回放"
        return original_replay(request_arg)

    monkeypatch.setattr(retry, "_replay", _blocking_replay)
    replay_errors: list[BaseException] = []

    def _run_replay() -> None:
        try:
            retry.add(request)
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            replay_errors.append(exc)

    replay_thread = threading.Thread(target=_run_replay, daemon=True)
    replay_thread.start()
    assert stale_ledger_read.wait(30), "回放未读取到旧账本"

    # 模拟对象存储暂时不可删除：数据库删除已经提交，但暂存目录保留供稍后重试。
    original_cleanup = AssetStore.cleanup_staging

    def _failing_cleanup(self, user_id_arg, request_id_arg, owner_token) -> bool:
        raise OSError("staging locked")

    monkeypatch.setattr(AssetStore, "cleanup_staging", _failing_cleanup)
    first = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )
    assert first.complete is False
    assert _staging_files(asset_store)

    monkeypatch.setattr(AssetStore, "cleanup_staging", original_cleanup)
    allow_replay_lock.set()
    replay_thread.join(30)

    assert not replay_thread.is_alive()
    assert replay_errors, "删除后的回放必须失败，不能伪装成功"

    second = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )
    assert second.complete is True
    assert _count(database, Memory, user_id) == 0
    assert _objects(asset_store) == []
    assert _staging_files(asset_store) == []


def test_delete_cleans_staging_left_by_failed_publish(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """publish 失败会遗留暂存私有图片；Delete 必须同时清理正式对象与暂存目录。"""
    user_id = _uid("u")
    run_id = _uid("r")

    def _failing_publish(staged: Path, final: Path) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr("masm.storage.assets._publish_object", _failing_publish)

    with pytest.raises(OSError):
        _add(
            AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
            run_id,
            user_id,
        )

    # 数据库已提交（账本 COMMITTED），正式对象未发布，暂存里的私有图片仍在。
    assert _count(database, Memory, user_id) == 1
    assert _objects(asset_store) == []
    assert _staging_files(asset_store), "publish 失败后暂存文件应当仍在"

    report = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert report.complete is True
    assert _count(database, Memory, user_id) == 0
    assert _asset_uris(database, user_id, run_id) == []
    assert _objects(asset_store) == []
    assert _staging_files(asset_store) == []
    assert MemoryRepository(database).pending_deletion_uris(user_id, run_id) == []


def test_staging_cleanup_failure_is_persisted_and_retried(
    database: Database, asset_store: AssetStore, settings, embeddings, monkeypatch
) -> None:
    """暂存清理失败绝不能被静默吞掉：第一次 complete=False，恢复后重试才 complete=True。"""
    user_id = _uid("u")
    run_id = _uid("r")
    repo = MemoryRepository(database)

    def _failing_publish(staged: Path, final: Path) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr("masm.storage.assets._publish_object", _failing_publish)
    with pytest.raises(OSError):
        _add(
            AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings),
            run_id,
            user_id,
        )
    assert _staging_files(asset_store)

    original_cleanup = AssetStore.cleanup_staging

    def _explode(self, user_id_arg: str, request_id_arg: str, owner_token) -> bool:
        raise OSError("staging locked")

    monkeypatch.setattr(AssetStore, "cleanup_staging", _explode, raising=False)
    first = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert first.complete is False
    assert first.failed_object_uris, "暂存清理失败必须出现在待重试清单里"
    assert _staging_files(asset_store), "失败时暂存文件必须保留以便重试"

    monkeypatch.setattr(AssetStore, "cleanup_staging", original_cleanup, raising=False)
    second = _service(database, asset_store, settings, embeddings).delete_run(
        run_id, user_id=user_id
    )

    assert second.complete is True
    assert _staging_files(asset_store) == []
    assert repo.pending_deletion_uris(user_id, run_id) == []
