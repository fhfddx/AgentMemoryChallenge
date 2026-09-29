"""OpenAI-compatible 文本 Embedding Provider。"""

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from masm.providers.embeddings import EmbeddingProvider
from masm.providers.errors import ProviderResponseError, ProviderUnavailableError

MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class EmbeddingCallRecord:
    """不含原文与密钥的 Embedding 调用元数据。"""

    model: str
    batch_size: int
    attempts: int
    latency_ms: float
    succeeded: bool


def _bounded_attempts(value: int) -> int:
    if value < 1:
        raise ValueError("max_attempts 必须为正整数")
    return min(value, MAX_ATTEMPTS)


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """调用 OpenAI-compatible ``/embeddings`` 的固定空间 Provider。"""

    def __init__(
        self,
        *,
        model: str,
        model_version: str,
        dimensions: int,
        base_url: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if dimensions < 1:
            raise ValueError("dimensions 必须为正整数")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self.model_name = model
        self.model_version = model_version
        self.dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_attempts = _bounded_attempts(max_attempts)
        self.records: list[EmbeddingCallRecord] = []

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量生成文本向量，并按响应 index 恢复输入顺序。"""
        inputs = list(texts)
        if not inputs:
            return []
        body = {
            "model": self.model_name,
            "input": inputs,
            "dimensions": self.dimensions,
        }
        started = time.perf_counter()
        unavailable = False
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = self._post(body, len(inputs))
            except httpx.HTTPError:
                unavailable = True
            except (KeyError, TypeError, ValueError):
                unavailable = False
            else:
                self._record(inputs, attempt, started, succeeded=True)
                return result

        self._record(inputs, self._max_attempts, started, succeeded=False)
        if unavailable:
            raise ProviderUnavailableError("Embedding Provider 不可用") from None
        raise ProviderResponseError("Embedding Provider 响应无效") from None

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        """文本 Provider 不支持直接图片；由多模态组合 Provider 负责。"""
        raise ProviderResponseError("文本 Embedding Provider 不支持直接图片")

    def _post(self, body: dict[str, Any], expected: int) -> list[list[float]]:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            response = client.post(
                f"{self._base_url}/embeddings",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            return self._validate_data(response.json()["data"], expected)
        finally:
            if self._client is None:
                client.close()

    def _validate_data(self, data: object, expected: int) -> list[list[float]]:
        if not isinstance(data, list) or len(data) != expected:
            raise ValueError("Embedding 条目数量不匹配")
        ordered: list[list[float] | None] = [None] * expected
        for item in data:
            if not isinstance(item, dict):
                raise TypeError("Embedding 条目必须是对象")
            index = item["index"]
            vector = item["embedding"]
            if not isinstance(index, int) or not 0 <= index < expected:
                raise ValueError("Embedding index 越界")
            if ordered[index] is not None:
                raise ValueError("Embedding index 重复")
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise ValueError("Embedding 维度不匹配")
            values = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Embedding 包含非有限数值")
            ordered[index] = values
        if any(vector is None for vector in ordered):
            raise ValueError("Embedding index 缺失")
        return [vector for vector in ordered if vector is not None]

    def _record(
        self, texts: Sequence[str], attempts: int, started: float, *, succeeded: bool
    ) -> None:
        self.records.append(
            EmbeddingCallRecord(
                model=self.model_name,
                batch_size=len(texts),
                attempts=attempts,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                succeeded=succeeded,
            )
        )
