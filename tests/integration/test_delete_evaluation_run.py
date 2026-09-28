"""评测运行删除集成测试：数据库 + 对象存储，幂等且作用域明确。"""

import base64
import io
from pathlib import Path
from uuid import uuid4

from PIL import Image
from sqlalchemy import func, select

from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Asset, Memory, SourceMessage, User
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
        remaining = session.execute(
            select(Memory.request_id).where(Memory.user_id == user_a)
        ).scalars().all()
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


def test_delete_run_rejects_path_traversal(
    database: Database, asset_store: AssetStore, settings, embeddings, tmp_path
) -> None:
    """对象路径必须安全解析，禁止越界删除。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    store = AssetStore(tmp_path / "assets")
    store.base_dir.mkdir(parents=True, exist_ok=True)
    with database.session() as session:
        session.add(User(user_id="user-x"))
        session.flush()
        session.add(
            Asset(
                user_id="user-x",
                object_uri="../../outside.txt",
                media_type="image/png",
                content_hash="deadbeef",
                decoded_size=1,
            )
        )
        session.commit()

    report = _service(database, asset_store, settings, embeddings).delete_run(
        "missing-run", user_id="user-x"
    )

    assert report.complete is True
    assert outside.exists()
