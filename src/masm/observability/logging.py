"""结构化日志脱敏。

采用**字段白名单 + 逐字段形状约束**：只有 :data:`ALLOWED_LOG_FIELDS` 中列出的键允许
进入日志，其余键（含容器里的键）在任意嵌套层级都被整体丢弃；白名单字段本身也不再允许
任意容器透传，而是按字段名施加受控形状（例如 ``token_usage`` 只保留数值统计，
``error_code`` 只接受 :data:`INTERNAL_ERROR_CODES` 枚举成员）。**失败标识只有一个可信
来源**：``exc_info`` 中的异常类型；调用方提供的 ``failure`` 文本一律丢弃，因为它无法与
用户私有内容区分（例如 ``MyPasswordIsHunter2`` 这类无空格字符串）。自由文本（格式化后的
消息、异常文本）没有字段名，因此在白名单之外再做值级脱敏，覆盖 Bearer Token、API Key、
数据 URL、连接串等。
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

# 只允许这些字段进入日志（数量、延迟、状态、模型版本、token、成本、降级等聚合元数据）。
# 注意：不含 "failure"——调用方提供的失败文本一律丢弃，见 RedactingFormatter。
ALLOWED_LOG_FIELDS = frozenset(
    {
        "request_id",
        "user_id_hash",
        "endpoint",
        "status",
        "status_code",
        "latency_ms",
        "count",
        "candidate_count",
        "dedup_count",
        "evidence_count",
        "returned_count",
        "response_bytes",
        "request_tag",
        "runtime_profile",
        "channel_counts",
        "selector_candidate_count",
        "selector_selected_count",
        "selector_source_count",
        "selector_selected_source_count",
        "selector_fallback",
        "selector_abstained",
        "selector_failure_category",
        "selector_evidence_state",
        "selector_latency_ms",
        "channel",
        "model_name",
        "model_version",
        "prompt_version",
        "code_version",
        "tokens",
        "token_usage",
        "cost_usd",
        "degraded",
        "error_code",
        "agent_name",
        "attempts",
        "run_id",
        "event",
    }
)

# 固定事件名（事件代码）白名单：日志只能使用这些事件。
ALLOWED_EVENTS = frozenset(
    {
        "log.record",
        "http.request",
        "http.response",
        "search.completed",
        "add.completed",
        "model.call",
        "model.failed",
        "agent.degraded",
        "deletion.completed",
        "deletion.failed",
        "dependency.unhealthy",
    }
)

_REDACTED = "[REDACTED]"

# 值级脱敏规则（用于无字段名的自由文本）。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE), _REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), f"Bearer {_REDACTED}"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?access[_-]?key|access[_-]?key|password|token)"
            r"\s*[:=]\s*[^\s,;]+"
        ),
        rf"\1={_REDACTED}",
    ),
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*[^\s,;]+"), f"authorization={_REDACTED}"),
    (
        re.compile(r"(?i)\b(postgresql|postgres|mysql|redis|mongodb)(\+\w+)?://[^\s\"']+"),
        _REDACTED,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{8,}\b"), _REDACTED),
    # 长 base64 片段（图片、密钥载荷）。
    (re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b"), _REDACTED),
)


def redact_text(value: Any) -> str:
    """对自由文本做值级脱敏。"""
    text = value if isinstance(value, str) else repr(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# token_usage 只允许这些键，且取值必须是数值：这是唯一进入日志的聚合容器。
_ALLOWED_TOKEN_USAGE_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "input_tokens",
        "output_tokens",
    }
)

_ALLOWED_CHANNEL_COUNT_KEYS = frozenset(
    {"lexical", "text_vector", "image_vector", "metadata"}
)

# 内部错误码显式枚举：只有这些值允许作为 ``failure`` 进入日志。
INTERNAL_ERROR_CODES = frozenset(
    {
        "pending_physical_delete",
        "object_delete_failed",
        "lock_timeout",
        "dependency_unhealthy",
        "upstream_timeout",
    }
)

_REJECTED_PLACEHOLDER = "[REJECTED]"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | bytes | bytearray | bool | int | float)


def _is_token_statistics(value: Any) -> bool:
    """token_usage 必须是「受控键 -> 数值」的扁平映射。"""
    if not isinstance(value, Mapping):
        return False
    for key, item in value.items():
        if str(key) not in _ALLOWED_TOKEN_USAGE_KEYS:
            return False
        if not isinstance(item, int | float) or isinstance(item, bool):
            return False
    return True


def _channel_statistics(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    statistics: dict[str, int] = {}
    for key, item in value.items():
        name = str(key)
        if name not in _ALLOWED_CHANNEL_COUNT_KEYS:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            continue
        statistics[name] = item
    return statistics


def _failure_from_exception(exc: type[BaseException] | None) -> str | None:
    """异常类型名是唯一来自异常的可信失败标识（绝不使用 str(exc)）。"""
    if exc is None:
        return None
    return f"{exc.__module__}.{exc.__qualname__}"


def redact_fields(payload: Any) -> Any:
    """按字段白名单递归投影结构化数据；非白名单字段一律丢弃。

    两条规则同时生效：
    1. 键不在白名单内 -> 整个键值（含其内部结构）被丢弃，绝不因为「值是容器」就保留；
    2. 键在白名单内 -> 按字段形状收敛：标量字段拒绝容器，``token_usage`` 只留受控数值
       统计，字符串统一走值级脱敏。

    注意 ``failure`` **不在**白名单内：调用方提供的失败文本一律丢弃，失败标识只能由
    :class:`RedactingFormatter` 从 ``exc_info`` 的异常类型推导。
    """
    if isinstance(payload, Mapping):
        projected: dict[str, Any] = {}
        for key, value in payload.items():
            name = str(key)
            if name not in ALLOWED_LOG_FIELDS:
                continue
            if name == "token_usage":
                if _is_token_statistics(value):
                    projected[name] = dict(value)
                continue
            if name == "channel_counts":
                statistics = _channel_statistics(value)
                if statistics is not None:
                    projected[name] = statistics
                continue
            if name == "error_code":
                # 内部错误码必须是显式枚举成员；其他取值一律丢弃。
                if isinstance(value, str) and value in INTERNAL_ERROR_CODES:
                    projected[name] = value
                continue
            if isinstance(value, str):
                # 自由文本字段仍做值级脱敏（长度/延迟等数值保持原类型）。
                projected[name] = redact_text(value)
            elif value is None or isinstance(value, bool | int | float):
                projected[name] = value
            # 其余（任意容器）不进入日志：白名单标量字段不接受容器形状。
        return projected
    if _is_scalar(payload):
        return redact_text(payload)
    return _REJECTED_PLACEHOLDER


class RedactingFormatter(logging.Formatter):
    """在格式化阶段对消息参数与日志记录附加字段做脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        """只输出固定事件名与白名单结构化元数据。

        ``record.msg``、普通字符串参数与 ``str(exc)`` 一律不进入输出；本方法不修改
        LogRecord（不写入任何属性），因此同一记录可安全地被其他 handler 处理。
        """
        payload: dict[str, Any] = {"event": _event_name(record), "status": record.levelname}
        sources = _metadata_sources(record.args)
        extra = getattr(record, "extra", None)
        if isinstance(extra, Mapping):
            sources = [*sources, extra]
        for source in sources:
            for key, value in redact_fields(source).items():
                payload[key] = value
        for key in ALLOWED_LOG_FIELDS:
            if key in record.__dict__:
                value = record.__dict__[key]
                if isinstance(value, str | int | float | bool):
                    payload[key] = redact_fields({key: value})[key]
        if record.exc_info and record.exc_info[0] is not None:
            # 只从异常对象取类型名，绝不使用 str(exc) 或调用方提供的 failure 文本。
            failure = _failure_from_exception(record.exc_info[0])
            if failure is not None:
                payload["failure"] = failure
        return json.dumps(payload, ensure_ascii=False, default=str)


def _event_name(record: logging.LogRecord) -> str:
    event = getattr(record, "event", None)
    if isinstance(event, str) and event in ALLOWED_EVENTS:
        return event
    return "log.record"


def _metadata_sources(args: Any) -> list[Mapping[str, Any]]:
    """从 logging 参数中提取结构化 Mapping，忽略普通字符串参数与异常对象。"""
    if args is None:
        return []
    if isinstance(args, Mapping):
        return [args]
    if isinstance(args, tuple):
        return [item for item in args if isinstance(item, Mapping)]
    return []


_STD_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


def _as_tuple(args: Any) -> tuple[Any, ...]:
    if isinstance(args, tuple):
        return args
    if isinstance(args, Mapping):
        return (args,)
    return (args,)


def configure_logging(settings: Any = None, *, level: int = logging.INFO) -> None:
    """配置根日志：结构化输出且强制脱敏。"""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
