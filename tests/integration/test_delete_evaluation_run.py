"""评测运行删除集成测试：数据库 + 对象存储，幂等且作用域明确。"""

import base64
import io
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
from masm.storage.repositories import MemoryRepository


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
    if not store.base_dir.exists():
        return []
    return [path for path in store.base_dir.rglob("*") if path.is_file()]


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
    import threading

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
