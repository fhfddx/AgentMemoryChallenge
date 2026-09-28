"""结构化日志脱敏。

采用**字段白名单**：只有 :data:`ALLOWED_LOG_FIELDS` 中列出的键允许进入日志，其余键
（含嵌套结构里的键）一律丢弃。自由文本（格式化后的消息、异常文本）没有字段名，
因此在白名单之外再做值级脱敏，覆盖 Bearer Token、API Key、数据 URL、连接串等。
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
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


def _is_container(value: Any) -> bool:
    if isinstance(value, str | bytes | bytearray):
        return False
    return isinstance(value, Mapping | Sequence)


def redact_fields(payload: Any) -> Any:
    """按字段白名单递归投影结构化数据；非白名单字段一律丢弃。"""
    if isinstance(payload, Mapping):
        projected: dict[str, Any] = {}
        for key, value in payload.items():
            name = str(key)
            if _is_container(value):
                # 容器结构允许保留，但内部叶子字段仍逐层走白名单。
                projected[name] = redact_fields(value)
            elif name in ALLOWED_LOG_FIELDS:
                projected[name] = redact_fields(value)
        return projected
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, Sequence) and not isinstance(payload, bytes | bytearray):
        return [redact_fields(item) for item in payload]
    if isinstance(payload, bool | int | float) or payload is None:
        return payload
    return redact_text(payload)


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
