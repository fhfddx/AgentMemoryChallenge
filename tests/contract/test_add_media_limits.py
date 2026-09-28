"""图片媒体与大小限制契约测试（使用真实最小图片）。"""

import base64
import io
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from masm.api.app import create_app
from masm.config import Settings
from masm.storage.assets import AssetStore
from masm.storage.db import Database


def _encode(image_format: str, size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=image_format)
    return buffer.getvalue()


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def _payload(user_id: str, request_id: str, image_url: str) -> dict:
    return {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": "session-1",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a picture"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }


def _auth() -> dict:
    return {"X-Api-Key": "test-key"}


def test_real_images_accepted(client: TestClient) -> None:
    """真实 JPEG/PNG/WebP Data URL 被接受。"""
    for image_format, media_type in (
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
    ):
        response = client.post(
            "/add",
            json=_payload(
                f"u-{uuid4().hex}",
                f"r-{uuid4().hex}",
                _data_url(media_type, _encode(image_format)),
            ),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text


def test_invalid_base64_returns_400(client: TestClient) -> None:
    """非法 Base64（长度/填充非法）返回 400。"""
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}", f"r-{uuid4().hex}", "data:image/png;base64,A"),
        headers=_auth(),
    )
    assert response.status_code == 400


def test_unsupported_media_type_returns_422(client: TestClient) -> None:
    """不支持的媒体类型（GIF）返回 422。"""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 200, 0)).save(buffer, format="GIF")
    response = client.post(
        "/add",
        json=_payload(
            f"u-{uuid4().hex}", f"r-{uuid4().hex}", _data_url("image/gif", buffer.getvalue())
        ),
        headers=_auth(),
    )
    assert response.status_code == 422


def test_forged_magic_returns_422(client: TestClient) -> None:
    """伪造魔数的图片返回 422。"""
    forged = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}", f"r-{uuid4().hex}", _data_url("image/png", forged)),
        headers=_auth(),
    )
    assert response.status_code == 422


def test_truncated_image_returns_422(client: TestClient) -> None:
    """截断图片返回 422。"""
    data = _encode("PNG")
    response = client.post(
        "/add",
        json=_payload(
            f"u-{uuid4().hex}",
            f"r-{uuid4().hex}",
            _data_url("image/png", data[: len(data) // 3]),
        ),
        headers=_auth(),
    )
    assert response.status_code == 422


def test_format_mismatch_returns_422(client: TestClient) -> None:
    """声明类型与实际格式不一致返回 422。"""
    response = client.post(
        "/add",
        json=_payload(
            f"u-{uuid4().hex}", f"r-{uuid4().hex}", _data_url("image/png", _encode("JPEG"))
        ),
        headers=_auth(),
    )
    assert response.status_code == 422


def test_single_image_over_limit_returns_413(
    database: Database, database_url: str, tmp_path: Path
) -> None:
    """单图超过配置上限返回 413。"""
    data = _encode("PNG", (64, 64))
    small = Settings(
        database_url=database_url, api_keys=("test-key",), max_image_bytes=len(data) - 1
    )
    app = create_app(small, database=database, asset_store=AssetStore(tmp_path / "assets"))
    with TestClient(app) as client:
        response = client.post(
            "/add",
            json=_payload(f"u-{uuid4().hex}", f"r-{uuid4().hex}", _data_url("image/png", data)),
            headers=_auth(),
        )
    assert response.status_code == 413


def test_total_images_over_limit_returns_413(
    database: Database, database_url: str, tmp_path: Path
) -> None:
    """单次 Add 图片总量超过上限返回 413。"""
    image_url = _data_url("image/png", _encode("PNG", (32, 32)))
    small = Settings(database_url=database_url, api_keys=("test-key",), max_add_image_bytes=10)
    app = create_app(small, database=database, asset_store=AssetStore(tmp_path / "assets"))
    with TestClient(app) as client:
        payload = {
            "request_id": f"r-{uuid4().hex}",
            "user_id": f"u-{uuid4().hex}",
            "session_id": "session-1",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        response = client.post("/add", json=payload, headers=_auth())
    assert response.status_code == 413
