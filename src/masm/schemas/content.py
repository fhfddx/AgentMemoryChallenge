"""多模态内容分片模型。

`Message.content` 与 `SearchRequest.query` 接受纯文本或保持原始顺序的内容数组。
内容数组元素是 `TextPart` 或 `ImageURLPart`，与官方多模态契约保持一致。
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 官方要求图片只接受 `data:image/...;base64,...` 数据 URL，不接受远程地址。
_DATA_IMAGE_URL_RE = re.compile(
    r"^data:image/[a-z0-9.+-]+;base64,[a-z0-9+/]+={0,2}$",
    re.IGNORECASE,
)


class TextPart(BaseModel):
    """文本内容分片。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _text_non_blank(cls, value: str) -> str:
        """文本必须非空且不能仅含空白字符。"""
        if not value.strip():
            raise ValueError("text 不能为空或仅含空白")
        return value


class ImageURL(BaseModel):
    """图片数据 URL（仅限 `data:image/...;base64,...`）。"""

    model_config = ConfigDict(extra="forbid")

    url: str

    @field_validator("url")
    @classmethod
    def _must_be_data_image_url(cls, value: str) -> str:
        """仅接受 data image URL，拒绝远程 URL 与其它格式。"""
        if not _DATA_IMAGE_URL_RE.match(value):
            raise ValueError("图片 URL 必须是 data:image/...;base64,... 数据 URL")
        return value


class ImageURLPart(BaseModel):
    """图片内容分片。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


# 内容分片联合类型：按 `type` 字段区分文本与图片。
ContentPart = Annotated[TextPart | ImageURLPart, Field(discriminator="type")]
