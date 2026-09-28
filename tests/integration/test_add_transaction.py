"""Add 最终提交事务、故障注入与 PROCESSING 租约测试。"""

import base64
import io
from datetime import timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

from masm.schemas.api import AddRequest
from masm.services.add_service import (
    AddConflictError,
    AddPreviouslyFailedError,
    AddService,
    utc_now,
)
from masm.storage.assets import AssetStore
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
        parts.append(
            {"type": "image_url", "image_url": {"url": _data_url(_encode(8 + index))}}
        )
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": parts}],
    )


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

    def put(self, user_id, request_id, image):  # type: ignore[override]
        self._calls += 1
        if self._calls >= self._fail_on:
            raise RuntimeError("injected asset failure")
        return super().put(user_id, request_id, image)


# ---------------------------------------------------------------- 事务原子性


def test_finalize_failure_leaves_no_partial_rows(database: Database) -> None:
    """最终数据库事务失败时不留下 Memory / SourceMessage / Asset 数据。"""
    repo = MemoryRepository(database)
    user_id, request_id = _uid("u"), _uid("r")
    repo.claim_request(user_id, request_id, "session-1", now=utc_now())

    broken = MemoryBundle(
        session_id="session-1",
        request_id=request_id,
        messages=[SourceMessageDraft(role="user", content=object(), position=0)],
        memories=[MemoryDraft(summary="ok", original_text="ok")],
    )
    with pytest.raises((StatementError, TypeError)):
        repo.finalize_request(user_id, request_id, broken)

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
        repo.finalize_request(user_id, request_id, bundle)
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


def test_shared_existing_object_not_deleted(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """既有共享对象不会被后续失败请求删除。"""
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
    objects = list(asset_store.base_dir.rglob("*.png"))
    assert len(objects) == 1

    failing_repo = MemoryRepository(database)
    monkeypatch_target = failing_repo

    def _boom(*args, **kwargs):
        raise RuntimeError("injected db failure")

    monkeypatch_target.finalize_request = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        AddService(failing_repo, asset_store, settings).add(_image_only_request())

    # 该对象已被先前提交引用，不得删除。
    assert objects[0].exists()


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


def test_failed_request_is_not_replayed_as_success(
    database: Database, asset_store: AssetStore, settings
) -> None:
    """FAILED 状态不得伪装成功。"""
    repo = MemoryRepository(database)
    service = AddService(repo, asset_store, settings)
    user_id, request_id = _uid("u"), _uid("r")
    repo.claim_request(user_id, request_id, "session-1", now=utc_now())
    repo.mark_request(user_id, request_id, "FAILED", now=utc_now(), expect="PROCESSING")

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
