"""图片严格解码与本地对象存储（不连接付费服务）。"""

import base64
import binascii
import contextlib
import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from masm.schemas.internal import AssetRef, DecodedImage

# 只允许 JPEG、PNG、WebP。
SUPPORTED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.DOTALL)

_EXT_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Pillow 识别的格式名到 MIME 的映射。
_PILLOW_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class MediaValidationError(ValueError):
    """媒体校验失败（携带 HTTP 状态码）。"""

    status_code = 422


class InvalidImageDataError(MediaValidationError):
    """非法 Base64 或畸形 data URL。"""

    status_code = 400


class UnsupportedImageTypeError(MediaValidationError):
    """不支持的媒体类型、无法解析或实际格式不匹配。"""

    status_code = 422


class ImageTooLargeError(MediaValidationError):
    """图片超过大小限制。"""

    status_code = 413


class AssetPathError(ValueError):
    """对象路径越出存储根目录（防御性检查，正常情况下不应触发）。"""


def estimated_decoded_size(payload: str) -> int:
    """由 Base64 文本长度上界估算解码后字节数，用于解码前的内存保护。"""
    return (len(payload) // 4) * 3


def _parse_image(data: bytes) -> tuple[str, int, int]:
    """使用 Pillow 实际解析并 verify 图片，返回 (media_type, width, height)。"""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = probe.format
            # verify() 校验结构完整性，可捕获截断与伪造魔数。
            probe.verify()
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise UnsupportedImageTypeError("图片无法解析或已损坏") from exc

    media_type = _PILLOW_FORMAT_TO_MIME.get(image_format or "")
    if media_type is None:
        raise UnsupportedImageTypeError(f"不支持的图片格式: {image_format}")
    return media_type, width, height


def decode_image_data_url(value: str, max_bytes: int) -> DecodedImage:
    """严格解码并校验图片 data URL。

    校验顺序：data URL 形状（400）、Base64 长度预检（413）、Base64 合法性（400）、
    媒体类型白名单（422）、单图大小（413）、Pillow 实际解析（422）、格式一致（422）。
    """
    match = _DATA_URL_RE.match(value)
    if match is None:
        raise InvalidImageDataError("无效的图片 data URL")
    declared_type: str = match.group(1).lower()
    payload = match.group(2)

    # 解码前按 Base64 长度预检查潜在大小，避免为超大载荷分配内存。
    if estimated_decoded_size(payload) > max_bytes:
        raise ImageTooLargeError("图片超过单图大小上限")

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidImageDataError("无效的 Base64 数据") from exc

    if declared_type not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedImageTypeError(f"不支持的图片类型: {declared_type}")
    if len(data) > max_bytes:
        raise ImageTooLargeError("图片超过单图大小上限")

    actual_type, width, height = _parse_image(data)
    if actual_type != declared_type:
        raise UnsupportedImageTypeError("图片实际格式与声明类型不一致")

    return DecodedImage(
        media_type=declared_type,
        data=data,
        decoded_size=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
    )


class AssetStore:
    """本地内容寻址对象存储。

    对象键由「user_id 的 SHA-256 命名空间 + 内容 SHA-256」生成，绝不直接使用原始
    user_id/request_id 作为路径；写入使用临时文件 + 原子替换。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _namespace(self, user_id: str) -> str:
        """由 user_id 派生稳定命名空间，消除路径穿越与特殊字符。"""
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def _resolve_within_root(self, relative: str) -> Path:
        """解析相对键为绝对路径，并验证其位于 base_dir 内。"""
        candidate = (self._base_dir / relative).resolve()
        if candidate != self._base_dir and self._base_dir not in candidate.parents:
            raise AssetPathError(f"对象路径越出存储根目录: {relative}")
        return candidate

    def put(self, user_id: str, request_id: str, image: DecodedImage) -> AssetRef:
        """写入对象并返回 AssetRef（含 created 状态）。

        request_id 保留给未来暂存/清理语义，当前内容寻址存储不依赖它。
        """
        extension = _EXT_BY_MEDIA_TYPE[image.media_type]
        content_hash = hashlib.sha256(image.data).hexdigest()
        object_key = f"{self._namespace(user_id)}/{content_hash}{extension}"
        target = self._resolve_within_root(object_key)

        created = not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        if created:
            self._atomic_write(target, image.data)

        return AssetRef(
            object_uri=object_key,
            media_type=image.media_type,
            content_hash=content_hash,
            decoded_size=image.decoded_size,
            width=image.width,
            height=image.height,
            created=created,
        )

    def remove(self, object_uri: str) -> None:
        """删除一个对象文件；路径必须位于 base_dir 内。"""
        target = self._resolve_within_root(object_uri)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """通过同目录临时文件 + os.replace 原子落盘，避免半文件。"""
        handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
