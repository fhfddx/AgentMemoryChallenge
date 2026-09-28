"""Add 最终提交事务、故障注入、所有权隔离与 PROCESSING 租约测试。"""

import base64
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

from masm.config import Settings
from masm.schemas.api import AddRequest
from masm.services.add_service import (
    AddConflictError,
    AddPreviouslyFailedError,
    AddService,
    utc_now,
)
from masm.storage.assets import (
    AssetStore,
    ImageTooLargeError,
    InvalidImageDataError,
    decode_image_data_url,
)
from masm.storage.db import Database
from masm.storage.models import Asset, Memory, SourceMessage
from masm.storage.repositories import LedgerStateError, MemoryRepository
from masm.storage.types import MemoryBundle, MemoryDraft, SourceMessageDraft


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _count(database: Database, model: type, user_id: str) -> int:
    with database.session() as session:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )


def _asset_uris(database: Database, user_id: str) -> list[str]:
    with database.session() as session:
        return list(
            session.execute(select(Asset.object_uri).where(Asset.user_id == user_id)).scalars()
        )


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def _request(request_id: str, user_id: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": "hello memory"}],
    )


def _encode(size: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url(data: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


def _image_request(request_id: str, user_id: str, count: int = 1) -> AddRequest:
    parts: list[dict] = [{"type": "text", "text": "look at this"}]
    for index in range(count):
        parts.append({"type": "image_url", "image_url": {"url": _data_url(_encode(8 + index))}})
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": parts}],
    )


def _decoded(size: int = 16):
    return decode_image_data_url(_data_url(_encode(size)), 10 * 1024 * 1024)


class _FakeClock:
    """可注入时钟，测试无需真实 sleep。"""

    def __init__(self) -> None:
        self.value = utc_now()

    def __call__(self):
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class _FailingAssetStore(AssetStore):
    """在第 N 次 put 时注入失败。"""

    def __init__(self, base_dir, fail_on: int) -> None:
        super().__init__(base_dir)
        self._fail_on = fail_on
        self._calls = 0

    def put(self, user_id, request_id, owner_token, image):  # type: ignore[override]
        self._calls += 1
        if self._calls >= self._fail_on:
            raise RuntimeError("injected asset failure")
        return super().put(user_id, request_id, owner_token, image)


# ---------------------------------------------------------------- 事务原子性


def test_finalize_failure_leaves_no_partial_rows(database: Database) -> None:
    """最终数据库事务失败时不留下 Memory / SourceMessage / Asset 数据。"""
    repo = MemoryRepository(database)
    user_id, request_id = _uid("u"), _uid("r")
    token = repo.claim_request(user_id, request_id, "session-1", now=utc_now())
    assert token is not None

    broken = MemoryBundle(
        session_id="session-1",
        request_id=request_id,
        messages=[SourceMessageDraft(role="user", content=object(), position=0)],
        memories=[MemoryDraft(summary="ok", original_text="ok")],
    )
    with pytest.raises((StatementError, TypeError)):
        repo.finalize_request(user_id, request_id, broken, owner_token=token)

    assert _count(database, Memory, user_id) == 0
    assert _count(database, SourceMessage, user_id) == 0
    assert _count(database, Asset, user_id) == 0


def test_finalize_requires_processing_ledger(database: Database) -> None:
    """账本不存在时 finalize_request 拒绝提交且不留下数据。"""
    repo = MemoryRepository(database)
    user_id, request_id = _uid("u"), _uid("r")
    bundle = MemoryBundle(
        session_id="session-1",
        request_id=request_id,
        memories=[MemoryDraft(summary="x", original_text="x")],
    )
    with pytest.raises(LedgerStateError):
        repo.finalize_request(user_id, request_id, bundle, owner_token=utc_now())
    assert _count(database, Memory, user_id) == 0


def test_finalize_rejects_stale_owner_token(database: Database) -> None:
    """所有者标识不匹配时拒绝提交。"""
    repo = MemoryRepository(database)
    user_id, request_id = _uid("u"), _uid("r")
    token = repo.claim_request(user_id, request_id, "session-1", now=utc_now())
    assert token is not None

    bundle = MemoryBundle(
        session_id="session-1",
        request_id=request_id,
        memories=[MemoryDraft(summary="x", original_text="x")],
    )
    with pytest.raises(LedgerStateError):
        repo.finalize_request(
            user_id, request_id, bundle, owner_token=token + timedelta(seconds=1)
        )
    assert _count(database, Memory, user_id) == 0


# ---------------------------------------------------------------- 故障注入


def test_db_failure_cleans_new_object_files(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    asset_store: AssetStore,
    settings,
) -> None:
    """数据库失败时清理本次新创建的对象文件。"""
    service = AddService(MemoryRepository(database), asset_store, settings)
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("injected db failure")

    monkeypatch.setattr(MemoryRepository, "finalize_request", _boom)
    with pytest.raises(RuntimeError):
        service.add(request)

    assert list(asset_store.base_dir.rglob("*.png")) == []
    assert _count(database, Memory, user_id) == 0


def test_object_write_failure_cleans_previous_new_files(
    database: Database, settings, tmp_path
) -> None:
    """对象写入过程中失败时清理此前已写入的新文件。"""
    store = _FailingAssetStore(tmp_path / "assets", fail_on=2)
    service = AddService(MemoryRepository(database), store, settings)
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id, count=2)

    with pytest.raises(RuntimeError):
        service.add(request)

    assert list(store.base_dir.rglob("*.png")) == []
    assert _count(database, Memory, user_id) == 0


def test_published_object_not_deleted_by_later_failure(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """已提交请求发布的对象不会被后续失败请求删除。"""
    user_id = _uid("u")
    url = _data_url(_encode(12))

    def _image_only_request() -> AddRequest:
        return AddRequest(
            request_id=_uid("r"),
            user_id=user_id,
            session_id="session-1",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": url}}],
                }
            ],
        )

    AddService(MemoryRepository(database), asset_store, settings).add(_image_only_request())
    published = [path for path in asset_store.base_dir.rglob("*.png")]
    assert len(published) == 1

    failing_repo = MemoryRepository(database)

    def _boom(*args, **kwargs):
        raise RuntimeError("injected db failure")

    failing_repo.finalize_request = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        AddService(failing_repo, asset_store, settings).add(_image_only_request())

    assert published[0].exists()


def test_concurrent_same_content_one_db_failure_keeps_object(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    asset_store: AssetStore,
    settings,
) -> None:
    """两个不同 request_id、同一用户、相同图片并发写入：一个 DB 提交失败，成功后对象仍在。"""
    user_id = _uid("u")
    url = _data_url(_encode(14))
    failing_id, ok_id = _uid("r"), _uid("r")

    original = MemoryRepository.finalize_request

    def _conditional(self, uid: str, rid: str, bundle, *, owner_token):
        if rid == failing_id:
            raise RuntimeError("injected db failure")
        return original(self, uid, rid, bundle, owner_token=owner_token)

    monkeypatch.setattr(MemoryRepository, "finalize_request", _conditional)
    barrier = threading.Barrier(2)

    def _run(rid: str) -> None:
        request = AddRequest(
            request_id=rid,
            user_id=user_id,
            session_id="session-1",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": url}}],
                }
            ],
        )
        barrier.wait()
        service = AddService(MemoryRepository(database), asset_store, settings)
        if rid == failing_id:
            with pytest.raises(RuntimeError):
                service.add(request)
        else:
            service.add(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run, rid) for rid in (failing_id, ok_id)]
        for future in futures:
            future.result()

    objects = [path for path in asset_store.base_dir.rglob("*.png")]
    assert len(objects) == 1
    assert objects[0].exists()


# ---------------------------------------------------------------- 所有权隔离


def test_takeover_isolates_old_owner_resources(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    asset_store: AssetStore,
    settings,
) -> None:
    """确定的接管时序：旧处理者不得提交，也不得清理新所有者资源。"""
    clock = _FakeClock()
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id)
    old_token = clock()

    old_blocked = threading.Event()
    release_old = threading.Event()
    new_blocked = threading.Event()
    release_new = threading.Event()

    original_finalize = MemoryRepository.finalize_request
    original_publish = AssetStore.publish

    def finalize(self, uid, rid, bundle, *, owner_token):
        if owner_token == old_token:
            old_blocked.set()
            release_old.wait(timeout=10)
        return original_finalize(self, uid, rid, bundle, owner_token=owner_token)

    def publish(self, uid, rid, owner_token):
        if owner_token != old_token:
            new_blocked.set()
            release_new.wait(timeout=10)
        return original_publish(self, uid, rid, owner_token)

    monkeypatch.setattr(MemoryRepository, "finalize_request", finalize)
    monkeypatch.setattr(AssetStore, "publish", publish)

    results: dict[str, object] = {}

    def _service() -> AddService:
        return AddService(
            MemoryRepository(database),
            asset_store,
            settings,
            clock=clock,
            processing_lease_seconds=60,
        )

    def _old() -> None:
        try:
            results["old"] = _service().add(request)
        except AddConflictError as exc:
            results["old_conflict"] = exc

    def _new() -> None:
        results["new"] = _service().add(request)

    old_thread = threading.Thread(target=_old)
    old_thread.start()
    assert old_blocked.wait(timeout=10), "旧处理者未能到达 finalize 前"

    clock.advance(120)
    new_thread = threading.Thread(target=_new)
    new_thread.start()
    assert new_blocked.wait(timeout=10), "新处理者未能完成 DB 提交"

    release_old.set()
    old_thread.join(timeout=10)
    assert not old_thread.is_alive()

    release_new.set()
    new_thread.join(timeout=10)
    assert not new_thread.is_alive()

    assert "old_conflict" in results
    assert "old" not in results
    new_response = results["new"]
    assert getattr(new_response, "success", None) is True

    repo = MemoryRepository(database)
    assert repo.get_ledger_status(user_id, request_id) == "COMMITTED"
    assert _count(database, Memory, user_id) == 1
    assert _count(database, SourceMessage, user_id) == 1

    uris = _asset_uris(database, user_id)
    assert uris
    for uri in uris:
        assert (asset_store.base_dir / uri).exists()


def test_stale_owner_failure_does_not_touch_new_owner(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    asset_store: AssetStore,
    settings,
) -> None:
    """旧处理者在接管后发生普通异常：不得删除新暂存，也不得把新账本标记 FAILED。"""
    clock = _FakeClock()
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id)
    old_token = clock()

    old_at_finalize = threading.Event()
    release_old = threading.Event()

    original_finalize = MemoryRepository.finalize_request

    def finalize(self, uid, rid, bundle, *, owner_token):
        if owner_token == old_token:
            old_at_finalize.set()
            release_old.wait(timeout=10)
            raise RuntimeError("injected old-owner failure")
        return original_finalize(self, uid, rid, bundle, owner_token=owner_token)

    monkeypatch.setattr(MemoryRepository, "finalize_request", finalize)

    failures: list[BaseException] = []

    def _old() -> None:
        service = AddService(
            MemoryRepository(database),
            asset_store,
            settings,
            clock=clock,
            processing_lease_seconds=60,
        )
        try:
            service.add(request)
        except RuntimeError as exc:
            failures.append(exc)

    old_thread = threading.Thread(target=_old)
    old_thread.start()
    assert old_at_finalize.wait(timeout=10)

    repo = MemoryRepository(database)
    clock.advance(120)
    new_token = repo.takeover_request(
        user_id, request_id, now=clock(), stale_before=clock() - timedelta(seconds=60)
    )
    assert new_token is not None
    asset_store.put(user_id, request_id, new_token, _decoded())

    release_old.set()
    old_thread.join(timeout=10)
    assert not old_thread.is_alive()
    assert failures

    # 新所有者暂存仍在，新账本仍为 PROCESSING（未被旧所有者标记 FAILED）。
    assert _files(asset_store.staging_dir(user_id, request_id, new_token))
    state = repo.get_ledger(user_id, request_id)
    assert state is not None
    assert state.status == "PROCESSING"
    assert state.owner_token == new_token


def test_stale_owner_cannot_mark_failed(database: Database) -> None:
    """旧所有者标识无法修改新所有者的账本状态。"""
    repo = MemoryRepository(database)
    user_id, request_id = _uid("u"), _uid("r")
    old_token = repo.claim_request(user_id, request_id, "session-1", now=utc_now())
    assert old_token is not None
    new_token = repo.takeover_request(
        user_id,
        request_id,
        now=old_token + timedelta(seconds=120),
        stale_before=old_token + timedelta(seconds=60),
    )
    assert new_token is not None

    assert repo.mark_request(user_id, request_id, "FAILED", owner_token=old_token) is False
    assert repo.get_ledger_status(user_id, request_id) == "PROCESSING"


# ---------------------------------------------------------------- 幂等恢复


def test_committed_then_crash_retry_replays(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """数据库已 COMMITTED 但响应未返回时，重试返回原提交结果。"""
    user_id, request_id = _uid("u"), _uid("r")
    request = _request(request_id, user_id)
    first = AddService(MemoryRepository(database), asset_store, settings).add(request)
    retry = AddService(MemoryRepository(database), asset_store, settings).add(request)
    assert first == retry
    assert _count(database, Memory, user_id) == 1


def test_committed_retry_publishes_owner_staging(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    asset_store: AssetStore,
    settings,
) -> None:
    """COMMITTED 后未 publish 就崩溃，重试仍能找到并发布该所有者的暂存对象。"""
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id)

    original_publish = AssetStore.publish

    def _boom(self, uid, rid, owner_token):
        raise RuntimeError("injected publish failure")

    monkeypatch.setattr(AssetStore, "publish", _boom)
    with pytest.raises(RuntimeError):
        AddService(MemoryRepository(database), asset_store, settings).add(request)

    repo = MemoryRepository(database)
    assert repo.get_ledger_status(user_id, request_id) == "COMMITTED"
    # 尚未发布：对象仍留在该所有者的暂存目录里。
    assert all("staging" in path.parts for path in asset_store.base_dir.rglob("*.png"))

    monkeypatch.setattr(AssetStore, "publish", original_publish)
    response = AddService(MemoryRepository(database), asset_store, settings).add(request)
    assert response.success is True

    uris = _asset_uris(database, user_id)
    assert uris
    for uri in uris:
        assert (asset_store.base_dir / uri).exists()


def test_failed_request_is_not_replayed_as_success(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """FAILED 状态不得伪装成功。"""
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings)
    user_id, request_id = _uid("u"), _uid("r")
    token = repo.claim_request(user_id, request_id, "session-1", now=utc_now())
    assert token is not None
    repo.mark_request(user_id, request_id, "FAILED", owner_token=token)

    with pytest.raises(AddPreviouslyFailedError):
        service.add(_request(request_id, user_id))
    assert _count(database, Memory, user_id) == 0


# ---------------------------------------------------------------- 租约


def test_fresh_processing_blocks_as_conflict(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """未过期的 PROCESSING 视为真实并发。"""
    clock = _FakeClock()
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings, clock=clock, processing_lease_seconds=60)
    user_id, request_id = _uid("u"), _uid("r")
    repo.claim_request(user_id, request_id, "session-1", now=clock())

    with pytest.raises(AddConflictError):
        service.add(_request(request_id, user_id))
    assert _count(database, Memory, user_id) == 0


def test_stale_processing_without_data_reprocesses(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """过期且无已提交数据时允许安全重新处理。"""
    clock = _FakeClock()
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings, clock=clock, processing_lease_seconds=60)
    user_id, request_id = _uid("u"), _uid("r")
    repo.claim_request(user_id, request_id, "session-1", now=clock())
    clock.advance(120)

    response = service.add(_request(request_id, user_id))
    assert response.success is True
    assert repo.get_ledger_status(user_id, request_id) == "COMMITTED"
    assert _count(database, Memory, user_id) == 1


def test_stale_processing_with_data_recovers_committed(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """过期但已有完整提交数据时恢复为 COMMITTED 并返回原结果。"""
    clock = _FakeClock()
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings, clock=clock, processing_lease_seconds=60)
    user_id, request_id = _uid("u"), _uid("r")
    repo.claim_request(user_id, request_id, "session-1", now=clock())
    repo.add_bundle(
        user_id,
        MemoryBundle(
            session_id="session-1",
            request_id=request_id,
            memories=[MemoryDraft(summary="recovered", original_text="recovered")],
        ),
    )
    clock.advance(120)

    response = service.add(_request(request_id, user_id))
    assert response.success is True
    assert repo.get_ledger_status(user_id, request_id) == "COMMITTED"
    assert _count(database, Memory, user_id) == 1


# ---------------------------------------------------------------- 媒体前置校验


def test_invalid_media_leaves_no_processing(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """非法媒体请求在 claim 之前失败，不留下 PROCESSING 账本。"""
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings)
    user_id, request_id = _uid("u"), _uid("r")
    request = AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}}
                ],
            }
        ],
    )

    with pytest.raises(InvalidImageDataError):
        service.add(request)
    assert repo.get_ledger_status(user_id, request_id) is None
    assert list(asset_store.base_dir.rglob("*")) == []


def test_oversize_media_leaves_no_processing(
    database: Database, database_url: str, tmp_path: Path
) -> None:
    """超限媒体请求在 claim 之前失败，不留下 PROCESSING 账本。"""
    repo = MemoryRepository(database)
    small = Settings(database_url=database_url, api_keys=("test-key",), max_add_image_bytes=1)
    store = AssetStore(tmp_path / "assets")
    service = AddService(repo, store, small)
    user_id, request_id = _uid("u"), _uid("r")
    request = _image_request(request_id, user_id)

    with pytest.raises(ImageTooLargeError):
        service.add(request)
    assert repo.get_ledger_status(user_id, request_id) is None
    assert list(store.base_dir.rglob("*")) == []
