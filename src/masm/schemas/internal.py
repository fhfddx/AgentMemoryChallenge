"""内部领域 Schema（Add 流水线内部使用，不进入官方响应）。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DecodedImage:
    """解码后的图片。"""

    media_type: str  # image/jpeg | image/png | image/webp
    data: bytes
    decoded_size: int
    content_hash: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class AssetRef:
    """已存储的图片对象引用。

    object_uri 是发布后的内容寻址键；created 表示本次 put 是否在本请求暂存区新建了文件，
    仅作信息用途（清理按请求作用域进行，不依赖该标记）。
    """

    object_uri: str
    media_type: str
    content_hash: str
    decoded_size: int
    width: int | None = None
    height: int | None = None
    created: bool = False
