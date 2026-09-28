"""图片解码与对象存储（基线使用本地内容寻址实现，不连接付费服务）。"""

import base64
import binascii
import hashlib
import re
from pathlib import Path

from masm.schemas.internal import AssetRef, DecodedImage

# 只允许 JPEG、PNG、WebP。
SUPPORTED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.DOTALL)

_EXT_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class MediaValidationError(ValueError):
    """媒体校验失败（携带 HTTP 状态码）。"""

    status_code = 422


class InvalidImageDataError(MediaValidationError):
    """非法 Base64 或畸形 data URL。"""

    status_code = 400


class UnsupportedImageTypeError(MediaValidationError):
    """不支持的媒体类型或实际格式不匹配。"""

    status_code = 422


class ImageTooLargeError(MediaValidationError):
    """图片超过大小限制。"""

    status_code = 413


def _sniff_media_type(data: bytes) -> str | None:
    """按魔数识别实际图片格式。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image_data_url(value: str, max_bytes: int) -> DecodedImage:
    """解码并严格校验图片 data URL。

    校验顺序：Base64 合法性（400）、媒体类型（422）、单图大小（413）、实际格式（422）。
    """
    match = _DATA_URL_RE.match(value)
    if match is None:
        raise InvalidImageDataError("无效的图片 data URL")
    declared_type: str = match.group(1).lower()
    payload = match.group(2)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidImageDataError("无效的 Base64 数据") from exc
    if declared_type not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedImageTypeError(f"不支持的图片类型: {declared_type}")
    if len(data) > max_bytes:
        raise ImageTooLargeError("图片超过单图大小上限")
    actual_type = _sniff_media_type(data)
    if actual_type != declared_type:
        raise UnsupportedImageTypeError("图片实际格式与声明类型不一致")
    return DecodedImage(
        media_type=declared_type,
        data=data,
        decoded_size=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
    )


class AssetStore:
    """本地内容寻址对象存储。

    对象键由内容哈希生成，写入天然幂等；同一内容只落盘一份。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def put(self, user_id: str, request_id: str, image: DecodedImage) -> AssetRef:
        """按内容哈希写入对象并返回 AssetRef。

        request_id 保留给未来暂存/清理语义，当前内容寻址存储不依赖它。
        """
        extension = _EXT_BY_MEDIA_TYPE[image.media_type]
        object_key = f"{user_id}/{image.content_hash}{extension}"
        path = self._base_dir / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(image.data)
        return AssetRef(
            object_uri=object_key,
            media_type=image.media_type,
            content_hash=image.content_hash,
            decoded_size=image.decoded_size,
            width=image.width,
            height=image.height,
        )
