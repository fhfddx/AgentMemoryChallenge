"""AssetStore 路径隔离与原子写测试。"""

import hashlib
from pathlib import Path

import pytest

from masm.schemas.internal import DecodedImage
from masm.storage.assets import AssetPathError, AssetStore


def _image(data: bytes = b"unit-test-image-bytes") -> DecodedImage:
    return DecodedImage(
        media_type="image/png",
        data=data,
        decoded_size=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
        width=1,
        height=1,
    )


@pytest.mark.parametrize(
    "user_id",
    [
        "../escaped",
        "a/b/c",
        "/absolute/path/user",
        "normal-user",
        "..\\windows\\traversal",
        "user..name",
        "",
    ],
)
def test_objects_stay_within_store_root(tmp_path: Path, user_id: str) -> None:
    """任意 user_id 都只能落到存储根目录内。"""
    store = AssetStore(tmp_path / "assets")
    ref = store.put(user_id, "req-1", _image())
    final = (store.base_dir / ref.object_uri).resolve()
    assert final.exists()
    assert store.base_dir in final.parents


def test_object_key_never_contains_raw_user_id(tmp_path: Path) -> None:
    """对象键使用哈希命名空间，不包含原始 user_id 片段。"""
    store = AssetStore(tmp_path / "assets")
    ref = store.put("../escaped", "req-1", _image())
    assert ".." not in ref.object_uri
    assert "escaped" not in ref.object_uri
    assert "/" in ref.object_uri  # 命名空间/文件名


def test_put_reports_created_only_once(tmp_path: Path) -> None:
    """同一内容第二次写入不再是新建。"""
    store = AssetStore(tmp_path / "assets")
    first = store.put("user-1", "req-1", _image())
    second = store.put("user-1", "req-2", _image())
    assert first.created is True
    assert second.created is False
    assert first.object_uri == second.object_uri


def test_remove_deletes_object(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    ref = store.put("user-1", "req-1", _image())
    target = (store.base_dir / ref.object_uri).resolve()
    store.remove(ref.object_uri)
    assert not target.exists()


def test_remove_rejects_path_traversal(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    with pytest.raises(AssetPathError):
        store.remove("../../outside.txt")


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    store.put("user-1", "req-1", _image())
    assert list(store.base_dir.rglob(".tmp-*")) == []


def test_written_content_matches_input(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    payload = b"exact-image-payload"
    ref = store.put("user-1", "req-1", _image(payload))
    assert (store.base_dir / ref.object_uri).read_bytes() == payload
