"""AssetStore 路径隔离、request-scoped 暂存与并发发布测试。"""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from masm.schemas.internal import DecodedImage
from masm.storage.assets import AssetStore


def _image(data: bytes = b"unit-test-image-bytes") -> DecodedImage:
    return DecodedImage(
        media_type="image/png",
        data=data,
        decoded_size=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
        width=1,
        height=1,
    )


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _norm(path: Path) -> str:
    text = str(Path(path).resolve())
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(text)


def _within(base: Path, path: Path) -> bool:
    base_text = _norm(base)
    path_text = _norm(path)
    return path_text == base_text or path_text.startswith(base_text + os.sep)


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
    """任意 user_id 都只能落到存储根目录内（暂存与发布后均如此）。"""
    root = tmp_path / "assets"
    store = AssetStore(root)
    store.put(user_id, "req-1", _image())
    store.publish(user_id, "req-1")

    assert [entry for entry in tmp_path.iterdir() if entry.name != "assets"] == []
    written = _files(root)
    assert written
    for path in written:
        assert _within(root, path)


def test_object_key_never_contains_raw_user_id(tmp_path: Path) -> None:
    """对象键使用哈希命名空间，不包含原始 user_id 片段。"""
    store = AssetStore(tmp_path / "assets")
    ref = store.put("../escaped", "req-1", _image())
    assert ".." not in ref.object_uri
    assert "escaped" not in ref.object_uri
    assert "/" in ref.object_uri


def test_put_stages_and_publish_moves_to_final(tmp_path: Path) -> None:
    """写入先进入本请求暂存目录，发布后落到内容寻址最终位置。"""
    store = AssetStore(tmp_path / "assets")
    ref = store.put("user-1", "req-1", _image())

    staged = _files(store.staging_dir("user-1", "req-1"))
    assert len(staged) == 1
    assert not (store.base_dir / ref.object_uri).exists()

    store.publish("user-1", "req-1")
    assert (store.base_dir / ref.object_uri).exists()
    assert store.staging_dir("user-1", "req-1").exists() is False


def test_discard_only_removes_own_staging(tmp_path: Path) -> None:
    """丢弃只影响本请求暂存，不影响其它请求。"""
    store = AssetStore(tmp_path / "assets")
    store.put("user-1", "req-1", _image(b"one"))
    store.put("user-1", "req-2", _image(b"two"))

    store.discard("user-1", "req-1")
    assert _files(store.staging_dir("user-1", "req-1")) == []
    assert len(_files(store.staging_dir("user-1", "req-2"))) == 1


def test_publish_is_idempotent(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    ref = store.put("user-1", "req-1", _image())
    store.publish("user-1", "req-1")
    store.publish("user-1", "req-1")
    assert (store.base_dir / ref.object_uri).exists()


def test_published_object_survives_other_request_discard(tmp_path: Path) -> None:
    """并发同内容：一个请求丢弃暂存时，另一个请求已发布的对象必须保留。"""
    store = AssetStore(tmp_path / "assets")
    payload = b"shared-content"
    first = store.put("user-1", "req-1", _image(payload))
    store.publish("user-1", "req-1")
    final = store.base_dir / first.object_uri
    assert final.exists()

    store.put("user-1", "req-2", _image(payload))
    store.discard("user-1", "req-2")
    assert final.exists()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    store.put("user-1", "req-1", _image())
    assert list(store.base_dir.rglob(".tmp-*")) == []


def test_written_content_matches_input(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    payload = b"exact-image-payload"
    ref = store.put("user-1", "req-1", _image(payload))
    store.publish("user-1", "req-1")
    assert (store.base_dir / ref.object_uri).read_bytes() == payload


def test_concurrent_first_write_same_namespace_stress(tmp_path: Path) -> None:
    """多轮压力：并发首次创建同一用户/请求目录不得出现路径竞态异常。"""
    store = AssetStore(tmp_path / "assets")
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(seed: int) -> None:
        try:
            barrier.wait()
            for round_index in range(5):
                store.put(
                    "shared-user",
                    "shared-request",
                    _image(f"{seed}-{round_index}".encode()),
                )
        except BaseException as exc:  # noqa: BLE001 - 记录任何并发异常用于断言
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert errors == []
    written = _files(store.base_dir)
    assert written
    for path in written:
        assert _within(store.base_dir, path)


def test_concurrent_publish_same_content_is_safe(tmp_path: Path) -> None:
    """并发发布相同内容到同一最终键是安全的。"""
    store = AssetStore(tmp_path / "assets")
    payload = b"same-bytes"
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            store.put("shared-user", f"req-{index}", _image(payload))
            store.publish("shared-user", f"req-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert errors == []
    finals = _files(store.base_dir)
    assert len(finals) == 1
    assert finals[0].read_bytes() == payload
