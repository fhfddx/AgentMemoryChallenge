"""结构化模型 Provider 接口与 OpenAI 兼容实现。

Provider 负责超时、一次重试、JSON 解析与 Pydantic Schema 校验，并记录模型名、
Prompt 版本与调用结果；记录中不含任何原始内容。
"""

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_SECONDS = 30.0
# 首次调用 + 一次重试。
DEFAULT_MAX_ATTEMPTS = 2


class ModelUnavailableError(RuntimeError):
    """模型依赖不可用（传输失败或错误状态码）。"""


class StructuredOutputError(RuntimeError):
    """模型输出无法解析或无法通过 Schema 校验。"""


@dataclass(frozen=True)
class ModelRequest:
    """一次结构化模型调用的输入。

    ``timeout_seconds`` 与 ``max_attempts`` 为请求级覆盖项；为 None 时使用 Provider 配置。
    """

    prompt: str
    payload: Mapping[str, Any]
    model: str
    prompt_version: str
    timeout_seconds: float | None = None
    max_attempts: int | None = None


@dataclass(frozen=True)
class ModelCallRecord:
    """模型调用元数据（不含任何原始内容）。"""

    model: str
    prompt_version: str
    output_type: str
    attempts: int
    latency_ms: float
    succeeded: bool


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

    支持请求级超时、至多一次重试、JSON Schema 输出约束与 Pydantic 校验。
    传输层失败抛出 :class:`ModelUnavailableError`，输出不合规则抛出
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
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)

    def complete_json(self, request: ModelRequest, output_type: type[T]) -> T:
        """调用模型并把响应校验为 ``output_type``。"""
        body = self._build_body(request, output_type)
        attempts = max(1, request.max_attempts or self._max_attempts)
        timeout = request.timeout_seconds or self._timeout_seconds
        started = time.perf_counter()
        last_error: Exception | None = None
        transport_failure = False

        for attempt in range(1, attempts + 1):
            try:
                result = self._post(body, output_type, timeout)
            except httpx.HTTPError as exc:
                last_error, transport_failure = exc, True
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                last_error, transport_failure = exc, False
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
            raise ModelUnavailableError("模型依赖不可用") from last_error
        raise StructuredOutputError("模型输出无法通过 Schema 校验") from last_error

    def _build_body(self, request: ModelRequest, output_type: type[BaseModel]) -> dict[str, Any]:
        return {
            "model": request.model or self.model,
            "messages": [
                {"role": "system", "content": request.prompt},
                {
                    "role": "user",
                    "content": json.dumps(request.payload, ensure_ascii=False, default=str),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "schema": output_type.model_json_schema(),
                },
            },
        }

    def _post(
        self, body: dict[str, Any], output_type: type[T], timeout: float
    ) -> T:
        client = self._client or httpx.Client(timeout=timeout)
        try:
            response = client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return output_type.model_validate_json(content)
        finally:
            if self._client is None:
                client.close()
