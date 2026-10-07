"""不含载荷与身份的 Provider 失败诊断。"""

import json
import logging
import math
from dataclasses import dataclass
from enum import StrEnum

import httpx

_LOGGER = logging.getLogger("masm.provider")
_MAX_RETRY_AFTER_SECONDS = 86_400.0


class ProviderKind(StrEnum):
    """可诊断的外部 Provider 类型。"""

    LLM = "llm"
    EMBEDDING = "embedding"


class ProviderFailureCategory(StrEnum):
    """不包含异常文本的固定失败类别。"""

    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class ProviderFailureDiagnostics:
    """单次失败尝试的安全元数据。"""

    provider: ProviderKind
    attempt: int
    latency_ms: float
    category: ProviderFailureCategory
    status_code: int | None = None
    retry_after_seconds: float | None = None


def classify_provider_failure(
    *,
    provider: ProviderKind,
    attempt: int,
    latency_ms: float,
    error: BaseException,
) -> ProviderFailureDiagnostics:
    """只依据异常类型与受控响应元数据分类，绝不读取异常文本或响应正文。"""
    category = ProviderFailureCategory.INVALID_RESPONSE
    status_code = None
    retry_after_seconds = None
    if isinstance(error, httpx.TimeoutException):
        category = ProviderFailureCategory.TIMEOUT
    elif isinstance(error, httpx.HTTPStatusError):
        category = ProviderFailureCategory.HTTP_STATUS
        status_code = int(error.response.status_code)
        retry_after_seconds = _numeric_retry_after(error.response)
    elif isinstance(error, httpx.HTTPError):
        category = ProviderFailureCategory.TRANSPORT
    return ProviderFailureDiagnostics(
        provider=provider,
        attempt=max(1, int(attempt)),
        latency_ms=round(max(0.0, float(latency_ms)), 2),
        category=category,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )


def emit_provider_failure(value: ProviderFailureDiagnostics) -> None:
    """用固定字段序列化失败诊断，不把异常、URL、载荷或密钥交给日志系统。"""
    payload: dict[str, str | int | float] = {
        "event": "provider.failed",
        "provider": value.provider.value,
        "attempt": value.attempt,
        "latency_ms": value.latency_ms,
        "error_category": value.category.value,
    }
    if value.status_code is not None:
        payload["status_code"] = value.status_code
    if value.retry_after_seconds is not None:
        payload["retry_after_seconds"] = value.retry_after_seconds
    _LOGGER.warning(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _numeric_retry_after(response: httpx.Response) -> float | None:
    """只保留有界数字秒数；HTTP 日期或任意文本均不进入日志。"""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if not math.isfinite(seconds) or not 0 <= seconds <= _MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds
