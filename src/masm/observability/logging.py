"""结构化日志脱敏。

采用**字段白名单 + 逐字段形状约束**：只有 :data:`ALLOWED_LOG_FIELDS` 中列出的键允许
进入日志，其余键（含容器里的键）在任意嵌套层级都被整体丢弃；白名单字段本身也不再允许
任意容器透传，而是按字段名施加受控形状（例如 ``token_usage`` 只保留数值统计，
``failure`` 只接受异常类型名或内部错误码）。自由文本（格式化后的消息、异常文本）没有
字段名，因此在白名单之外再做值级脱敏，覆盖 Bearer Token、API Key、数据 URL、连接串等。
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

# 只允许这些字段进入日志（数量、延迟、状态、模型版本、token、成本、降级等聚合元数据）。
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
        "evidence_count",
        "channel",
        "model_name",
        "model_version",
        "prompt_version",
        "code_version",
        "tokens",
        "token_usage",
        "cost_usd",
        "degraded",
        "failure",
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

# failure 只允许异常类型名（如 TimeoutError）或内部错误码（如 E_UPSTREAM_TIMEOUT）。
_FAILURE_PATTERN = re.compile(r"\A(?:[A-Za-z_][A-Za-z0-9_]{0,63}|[A-Z][A-Z0-9_]{1,63})\Z")

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


def _redact_failure(value: Any) -> Any:
    """failure 只接受异常类型名或内部错误码；其余（可能含用户内容）一律替换。"""
    if isinstance(value, str) and _FAILURE_PATTERN.match(value):
        return value
    return _REJECTED_PLACEHOLDER


def redact_fields(payload: Any) -> Any:
    """按字段白名单递归投影结构化数据；非白名单字段一律丢弃。

    两条规则同时生效：
    1. 键不在白名单内 -> 整个键值（含其内部结构）被丢弃，绝不因为「值是容器」就保留；
    2. 键在白名单内 -> 按字段形状收敛：标量字段拒绝容器，``token_usage`` 只留受控数值
       统计，``failure`` 只留异常类型名/错误码，字符串统一走值级脱敏。
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
            if name == "failure":
                projected[name] = _redact_failure(value)
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
            # 只记录异常类型，绝不记录可能包含用户内容的 str(exc)。
            payload["failure"] = record.exc_info[0].__name__
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
