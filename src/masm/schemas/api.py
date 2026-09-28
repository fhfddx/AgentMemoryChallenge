"""官方 Add / Search 请求与响应契约。

字段名与响应外形必须与 Agent Memory Leaderboard 官方契约保持一致，不得修改。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from masm.schemas.content import ContentPart

# 官方要求 Search 支持 top_k=100；超出上限即校验失败。与 Settings.max_top_k 默认值一致。
MAX_TOP_K = 100


class Message(BaseModel):
    """一条保持原始顺序的消息。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str | list[ContentPart]
    timestamp: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("content")
    @classmethod
    def _content_non_empty(cls, value: str | list[ContentPart]) -> str | list[ContentPart]:
        """纯文本与内容数组都必须非空。"""
        if isinstance(value, str) and not value.strip():
            raise ValueError("content 不能为空")
        if isinstance(value, list) and not value:
            raise ValueError("content 数组不能为空")
        return value


class AddRequest(BaseModel):
    """官方 `/add` 请求。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class AddResponse(BaseModel):
    """官方 `/add` 响应。"""

    model_config = ConfigDict(extra="forbid")

    success: Literal[True]
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(BaseModel):
    """官方 `/search` 请求。"""

    model_config = ConfigDict(extra="forbid")

    query: str | list[ContentPart]
    options: list[str] | None = None
    user_id: str = Field(min_length=1)
    top_k: int = Field(ge=1, le=MAX_TOP_K, strict=True)

    @field_validator("query")
    @classmethod
    def _query_non_empty(cls, value: str | list[ContentPart]) -> str | list[ContentPart]:
        """纯文本与内容数组都必须非空。"""
        if isinstance(value, str) and not value.strip():
            raise ValueError("query 不能为空")
        if isinstance(value, list) and not value:
            raise ValueError("query 数组不能为空")
        return value

    @field_validator("options")
    @classmethod
    def _options_valid(cls, value: list[str] | None) -> list[str] | None:
        """options 可选；若提供则数组非空且每项非空白。"""
        if value is None:
            return value
        if not value:
            raise ValueError("options 数组不能为空")
        for option in value:
            if not option.strip():
                raise ValueError("options 不能包含空或纯空白字符串")
        return value


class MemoryEvidence(BaseModel):
    """Search 返回的单条记忆证据。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    content: str | list[ContentPart]
    score: float | None = None
    created_at: str | None = None

    @field_validator("content")
    @classmethod
    def _content_non_empty(cls, value: str | list[ContentPart]) -> str | list[ContentPart]:
        """content 必须非空：空字符串、纯空格、空数组均拒绝。"""
        if isinstance(value, str) and not value.strip():
            raise ValueError("content 不能为空")
        if isinstance(value, list) and not value:
            raise ValueError("content 数组不能为空")
        return value


class SearchResponse(BaseModel):
    """官方 `/search` 响应。"""

    model_config = ConfigDict(extra="forbid")

    data: list[MemoryEvidence]
