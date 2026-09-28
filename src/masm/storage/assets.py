"""图片严格解码与本地对象存储。

写入采用 request-scoped 暂存：对象先落到仅本请求可见的暂存目录，数据库事务提交成功后
再原子发布到内容寻址的最终位置。失败请求只丢弃自己的暂存目录，绝不删除共享对象。
"""

import base64
import binascii
import contextlib
import hashlib
import io
import os
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
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

_STAGING_DIRNAME = "staging"


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
    """估算 Base64 文本解码后的字节数。

    对合法长度按 padding 精确计算，因此「实际解码大小恰好等于上限」不会被误拒；
    非法长度随后会被 b64decode 拒绝，此处给出保守上界以防超大内存分配。
    """
    length = len(payload)
    if length == 0:
        return 0
    groups, remainder = divmod(length, 4)
    if remainder != 0:
        return groups * 3 + 3
    padding = 0
    if payload.endswith("=="):
        padding = 2
    elif payload.endswith("="):
        padding = 1
    return max(0, groups * 3 - padding)


def _strip_extended_prefix(text: str) -> str:
    """去掉 Windows 扩展长度路径前缀，避免 \\\\?\\ 造成的路径比较竞态。"""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\") :]
    if text.startswith(("\\\\?\\", "\\\\.\\")):
        return text[4:]
    return text


def _join_within(base: Path, relative: str) -> Path:
    """文本拼接 + 归一化，不调用 resolve()，避免首次并发建目录时的前缀竞态。"""
    joined = os.path.normpath(os.path.join(str(base), relative))
    return Path(_strip_extended_prefix(joined))


def _is_within(base: Path, candidate: Path) -> bool:
    base_text = os.path.normcase(_strip_extended_prefix(str(base)))
    candidate_text = os.path.normcase(_strip_extended_prefix(str(candidate)))
    return candidate_text == base_text or candidate_text.startswith(base_text + os.sep)


# Windows 上多线程并发替换同一目标路径会瞬时返回“拒绝访问”，需要有限重试。
_MOVE_ATTEMPTS = 6
_MOVE_BACKOFF_SECONDS = 0.02


def _publish_object(staged: Path, final: Path) -> None:
    """把暂存对象发布到最终内容寻址位置。

    文件名就是内容哈希，因此目标已存在时内容必然相同，直接丢弃暂存副本即可；
    否则做有限退避重试，以覆盖 Windows 并发替换同一路径时的瞬时拒绝访问。
    """
    if final.exists():
        with contextlib.suppress(OSError):
            staged.unlink()
        return
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            os.replace(staged, final)
            return
        except PermissionError:
            if final.exists():
                with contextlib.suppress(OSError):
                    staged.unlink()
                return
            if attempt == _MOVE_ATTEMPTS - 1:
                raise
            time.sleep(_MOVE_BACKOFF_SECONDS * (attempt + 1))


def _owner_text(owner_token: datetime) -> str:
    """把所有者标识规范化为与数据库往返无关的稳定文本（统一到 UTC）。"""
    return owner_token.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")


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
    """本地对象存储：request-scoped 暂存 + 内容寻址发布。

    路径只由 SHA-256 命名空间与内容哈希组成，绝不直接使用原始 user_id/request_id；
    所有拼接路径在写入前都验证位于 base_dir 内。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(_strip_extended_prefix(str(Path(base_dir).resolve())))

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _user_namespace(self, user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def _attempt_namespace(self, user_id: str, request_id: str, owner_token: datetime) -> str:
        """按「用户 + 请求 + 处理尝试（所有者标识）」隔离暂存命名空间。"""
        payload = f"{user_id}\x00{request_id}\x00{_owner_text(owner_token)}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _resolve_within_root(self, relative: str) -> Path:
        """解析相对键并验证其位于 base_dir 内。"""
        candidate = _join_within(self._base_dir, relative)
        if not _is_within(self._base_dir, candidate):
            raise AssetPathError(f"对象路径越出存储根目录: {relative}")
        return candidate

    def staging_dir(self, user_id: str, request_id: str, owner_token: datetime) -> Path:
        """本次处理尝试的暂存目录（仅该所有者可见）。"""
        namespace = self._attempt_namespace(user_id, request_id, owner_token)
        return self._resolve_within_root(f"{_STAGING_DIRNAME}/{namespace}")

    def put(
        self,
        user_id: str,
        request_id: str,
        owner_token: datetime,
        image: DecodedImage,
    ) -> AssetRef:
        """把对象写入本次处理尝试的暂存目录，返回最终内容寻址引用。"""
        extension = _EXT_BY_MEDIA_TYPE[image.media_type]
        content_hash = hashlib.sha256(image.data).hexdigest()
        filename = f"{content_hash}{extension}"
        object_uri = f"{self._user_namespace(user_id)}/{filename}"

        staging_dir = self.staging_dir(user_id, request_id, owner_token)
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = staging_dir / filename
        created = not staged.exists()
        if created:
            self._atomic_write(staged, image.data)

        return AssetRef(
            object_uri=object_uri,
            media_type=image.media_type,
            content_hash=content_hash,
            decoded_size=image.decoded_size,
            width=image.width,
            height=image.height,
            created=created,
        )

    def publish(self, user_id: str, request_id: str, owner_token: datetime) -> list[str]:
        """把本次处理尝试暂存的对象原子发布到内容寻址的最终位置。

        内容寻址天然幂等：目标已存在时内容必然相同，因此并发发布同一对象是安全的。
        """
        staging_dir = self.staging_dir(user_id, request_id, owner_token)
        if not staging_dir.exists():
            return []
        published: list[str] = []
        for staged in sorted(staging_dir.iterdir()):
            if not staged.is_file():
                continue
            relative = f"{self._user_namespace(user_id)}/{staged.name}"
            final = self._resolve_within_root(relative)
            final.parent.mkdir(parents=True, exist_ok=True)
            _publish_object(staged, final)
            published.append(relative)
        with contextlib.suppress(OSError):
            staging_dir.rmdir()
        return published

    def delete_object(self, relative: str) -> bool:
        """安全删除一个已发布对象。

        路径先经 ``_resolve_within_root`` 解析，越界（目录穿越）会抛 ``AssetPathError``；
        对象不存在时返回 False，便于调用方区分「已删除」与「本来就不存在」。
        """
        if not relative or relative.startswith(("/", "\\")) or Path(relative).is_absolute():
            raise AssetPathError("对象地址必须是存储根目录内的相对路径")
        path = self._resolve_within_root(relative)
        # 真实路径校验：防御 `..`、符号链接与目录 junction 越界。
        real_root = Path(os.path.realpath(self.base_dir))
        real_parent = Path(os.path.realpath(path.parent))
        real_path = Path(os.path.realpath(path))
        for candidate in (real_parent, real_path):
            if not _is_within(real_root, candidate):
                raise AssetPathError("对象真实路径越出存储根目录")
        if not path.exists():
            return False
        path.unlink()
        return True

    def discard(self, user_id: str, request_id: str, owner_token: datetime) -> None:
        """丢弃本次处理尝试的暂存目录；绝不触碰已发布或其它所有者的对象。"""
        staging_dir = self.staging_dir(user_id, request_id, owner_token)
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    def cleanup_staging(self, user_id: str, request_id: str, owner_token: datetime) -> bool:
        """合规删除：彻底移除该运行该所有者的暂存目录，返回是否已不存在。

        与 :meth:`discard` 不同，这里**绝不静默吞错**：任何失败都向上抛出，由调用方
        持久化为待重试状态（否则暂存内的私有图片会残留却报告删除成功）。
        """
        staging_dir = self.staging_dir(user_id, request_id, owner_token)
        if not staging_dir.exists():
            return False
        shutil.rmtree(staging_dir)
        return not staging_dir.exists()

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
