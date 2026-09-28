"""decode_image_data_url 严格校验测试（使用真实最小图片）。"""

import base64
import io

import pytest
from PIL import Image

from masm.storage.assets import (
    ImageTooLargeError,
    InvalidImageDataError,
    UnsupportedImageTypeError,
    decode_image_data_url,
    estimated_decoded_size,
)


def _encode(image_format: str, size: tuple[int, int] = (8, 8)) -> bytes:
    """用 Pillow 生成真实的最小图片字节。"""
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=image_format)
    return buffer.getvalue()


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_real_images_accepted(image_format: str, media_type: str) -> None:
    """真实 JPEG/PNG/WebP 被接受并解析出尺寸。"""
    decoded = decode_image_data_url(_data_url(media_type, _encode(image_format)), 10 * 1024 * 1024)
    assert decoded.media_type == media_type
    assert decoded.width == 8
    assert decoded.height == 8


def test_truncated_image_rejected() -> None:
    """截断图片被拒绝。"""
    data = _encode("PNG")
    with pytest.raises(UnsupportedImageTypeError):
        decode_image_data_url(_data_url("image/png", data[: len(data) // 3]), 10 * 1024 * 1024)


def test_forged_magic_bytes_rejected() -> None:
    """只有魔数、没有合法结构的伪造图片被拒绝。"""
    forged = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    with pytest.raises(UnsupportedImageTypeError):
        decode_image_data_url(_data_url("image/png", forged), 10 * 1024 * 1024)


def test_format_mismatch_rejected() -> None:
    """实际格式与声明 MIME 不一致被拒绝。"""
    with pytest.raises(UnsupportedImageTypeError):
        decode_image_data_url(_data_url("image/png", _encode("JPEG")), 10 * 1024 * 1024)


def test_unsupported_format_rejected() -> None:
    """GIF 等未授权格式被拒绝。"""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 200, 0)).save(buffer, format="GIF")
    with pytest.raises(UnsupportedImageTypeError):
        decode_image_data_url(_data_url("image/gif", buffer.getvalue()), 10 * 1024 * 1024)


def test_invalid_base64_rejected() -> None:
    with pytest.raises(InvalidImageDataError):
        decode_image_data_url("data:image/png;base64,A", 10 * 1024 * 1024)


def test_size_precheck_rejects_before_decode() -> None:
    """Base64 长度预检在解码前拒绝超大载荷。"""
    payload = base64.b64encode(b"\x00" * 3000).decode()
    with pytest.raises(ImageTooLargeError):
        decode_image_data_url(f"data:image/png;base64,{payload}", 100)


def test_decoded_size_limit_enforced() -> None:
    data = _encode("PNG", (64, 64))
    with pytest.raises(ImageTooLargeError):
        decode_image_data_url(_data_url("image/png", data), len(data) - 1)


def test_estimated_size_accounts_for_padding() -> None:
    """估算需扣除 padding，不能高估实际解码大小。"""
    payload = base64.b64encode(b"\x00\x00\x00\x00").decode()
    assert payload.endswith("==")
    assert estimated_decoded_size(payload) == 4


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    [("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_exact_size_limit_is_accepted(image_format: str, media_type: str) -> None:
    """解码大小恰好等于 max_bytes 时必须接受，少一字节才拒绝。"""
    data = _encode(image_format, (24, 24))
    decoded = decode_image_data_url(_data_url(media_type, data), len(data))
    assert decoded.decoded_size == len(data)
    with pytest.raises(ImageTooLargeError):
        decode_image_data_url(_data_url(media_type, data), len(data) - 1)
