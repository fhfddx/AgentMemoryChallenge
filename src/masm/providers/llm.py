"""结构化模型 Provider 接口与 OpenAI 兼容实现。

Provider 负责超时、至多一次重试、JSON 解析与 Pydantic Schema 校验，并把官方内容分片
转换为厂商格式。记录中不含任何原始内容。
"""

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from masm.providers.diagnostics import (
    ProviderKind,
    classify_provider_failure,
    emit_provider_failure,
)
from masm.providers.errors import ProviderResponseError, ProviderUnavailableError
from masm.schemas.content import ContentPart, TextPart

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_SECONDS = 30.0

# 首次调用 + 至多一次重试：任何 Provider 级或请求级配置都不能突破该上限。
MAX_ATTEMPTS = 2
DEFAULT_MAX_ATTEMPTS = MAX_ATTEMPTS


class ModelUnavailableError(ProviderUnavailableError):
    """模型依赖不可用（传输失败或错误状态码）。"""


class StructuredOutputError(ProviderResponseError):
    """模型输出无法解析或无法通过 Schema 校验。"""


@dataclass(frozen=True)
class ModelRequest:
    """一次结构化模型调用的输入。

    ``content`` 为保持原始顺序的官方内容分片，由 Provider 转换为厂商格式的多模态内容块；
    未提供 ``content`` 时，``payload`` 以 JSON 文本形式发送（用于结构化输入）。

    ``timeout_seconds`` 与 ``max_attempts`` 为请求级覆盖项；为 None 时使用 Provider 配置。
    """

    prompt: str
    model: str
    prompt_version: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    content: Sequence[ContentPart] | None = None
    timeout_seconds: float | None = None
    max_attempts: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class ModelCallRecord:
    """模型调用元数据（不含任何原始内容）。"""

    model: str
    prompt_version: str
    output_type: str
    attempts: int
    latency_ms: float
    succeeded: bool


def _effective_attempts(value: int | None, default: int) -> int:
    """把尝试次数约束在 ``1..MAX_ATTEMPTS``。

    低于 1 属于无效配置，明确拒绝；高于上限属于越界配置，安全截断。
    """
    if value is None:
        return min(default, MAX_ATTEMPTS)
    if value < 1:
        raise ValueError("max_attempts 必须为正整数")
    return min(value, MAX_ATTEMPTS)


def _to_content_block(part: ContentPart) -> dict[str, Any]:
    """官方内容分片 -> 厂商内容块（厂商格式只出现在 Provider 适配层内）。"""
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    return {"type": "image_url", "image_url": {"url": part.image_url.url}}


def _user_content(request: ModelRequest) -> str | list[dict[str, Any]]:
    """多模态分片转成保持顺序的 OpenAI 兼容内容块数组；结构化 payload 仍用 JSON 文本。"""
    if request.content is not None:
        return [_to_content_block(part) for part in request.content]
    return json.dumps(request.payload, ensure_ascii=False, default=str)


def _strict_json_schema(value: Any) -> Any:
    """把 Pydantic Schema 归一化为 OpenAI 严格输出支持的子集。"""
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _strict_json_schema(item)
        for key, item in value.items()
        if key != "default"
    }
    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["additionalProperties"] = False
        normalized["required"] = list(properties)
    return normalized


class StructuredLLM(ABC):
    """结构化模型 Provider：只返回通过 Schema 校验的 Pydantic 对象。"""

    model: str

    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    @abstractmethod
    def complete_json(self, request: ModelRequest, output_type: type[T]) -> T:
        """执行一次结构化调用，返回 ``output_type`` 实例。"""

    def _record(
        self,
        request: ModelRequest,
        output_type: type[BaseModel],
        *,
        attempts: int,
        latency_ms: float,
        succeeded: bool,
    ) -> None:
        self.records.append(
            ModelCallRecord(
                model=request.model or self.model,
                prompt_version=request.prompt_version,
                output_type=output_type.__name__,
                attempts=attempts,
                latency_ms=latency_ms,
                succeeded=succeeded,
            )
        )


class OpenAICompatibleLLM(StructuredLLM):
    """OpenAI 兼容的 JSON Schema 模式 Provider。

    支持请求级超时、至多一次重试、JSON Schema 输出约束与 Pydantic 校验；
    多模态 ``content`` 会原样转换为 ``text`` / ``image_url`` 内容块数组。
    传输层失败抛出 :class:`ModelUnavailableError`，输出不合规抛出
    :class:`StructuredOutputError`。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        super().__init__()
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._owned_client: httpx.Client | None = None
        self._client_lock = Lock()
        self._timeout_seconds = timeout_seconds
        self._max_attempts = _effective_attempts(max_attempts, DEFAULT_MAX_ATTEMPTS)

    @property
    def max_attempts(self) -> int:
        """实际生效的最大调用次数（1..MAX_ATTEMPTS）。"""
        return self._max_attempts

    def complete_json(self, request: ModelRequest, output_type: type[T]) -> T:
        """调用模型并把响应校验为 ``output_type``。"""
        body = self._build_body(request, output_type)
        attempts = _effective_attempts(request.max_attempts, self._max_attempts)
        timeout = request.timeout_seconds or self._timeout_seconds
        started = time.perf_counter()
        transport_failure = False

        for attempt in range(1, attempts + 1):
            attempt_started = time.perf_counter()
            try:
                result = self._post(body, output_type, timeout)
            except httpx.HTTPError as exc:
                transport_failure = True
                emit_provider_failure(
                    classify_provider_failure(
                        provider=ProviderKind.LLM,
                        attempt=attempt,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000.0,
                        error=exc,
                    )
                )
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                transport_failure = False
                emit_provider_failure(
                    classify_provider_failure(
                        provider=ProviderKind.LLM,
                        attempt=attempt,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000.0,
                        error=exc,
                    )
                )
            else:
                self._record(
                    request,
                    output_type,
                    attempts=attempt,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    succeeded=True,
                )
                return result

        self._record(
            request,
            output_type,
            attempts=attempts,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            succeeded=False,
        )
        if transport_failure:
            raise ModelUnavailableError("模型依赖不可用") from None
        raise StructuredOutputError("模型输出无法通过 Schema 校验") from None

    def _build_body(self, request: ModelRequest, output_type: type[BaseModel]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [
                {"role": "system", "content": request.prompt},
                {"role": "user", "content": _user_content(request)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "strict": True,
                    "schema": _strict_json_schema(output_type.model_json_schema()),
                },
            },
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    def _post(self, body: dict[str, Any], output_type: type[T], timeout: float) -> T:
        response = self._http_client().post(
            f"{self._base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return output_type.model_validate_json(content)

    def _http_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._owned_client is None:
                self._owned_client = httpx.Client(timeout=self._timeout_seconds)
            return self._owned_client

    def close(self) -> None:
        """关闭自身创建的连接池；注入的 Client 由调用方管理。"""
        with self._client_lock:
            if self._owned_client is not None:
                self._owned_client.close()
                self._owned_client = None
