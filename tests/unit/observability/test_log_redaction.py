"""日志脱敏单元测试：字段白名单 + 自由文本脱敏，覆盖嵌套结构、异常与 logging 参数。"""

import json
import logging

from masm.observability.logging import (
    ALLOWED_LOG_FIELDS,
    RedactingFormatter,
    redact_fields,
    redact_text,
)

_BASE64 = "data:image/png;base64," + "A" * 400


def test_allowed_fields_survive_and_unknown_fields_are_dropped() -> None:
    payload = {
        "request_id": "r-1",
        "latency_ms": 12.5,
        "status": "ok",
        "content": "raw message text",
        "api_key": "secret-key",
    }

    redacted = redact_fields(payload)

    assert redacted["request_id"] == "r-1"
    assert redacted["latency_ms"] == 12.5
    assert "content" not in redacted
    assert "api_key" not in redacted


def test_whitelist_is_explicit() -> None:
    assert {"request_id", "latency_ms", "status", "model_name", "degraded"} <= ALLOWED_LOG_FIELDS
    assert not {"content", "prompt", "api_key", "authorization"} & ALLOWED_LOG_FIELDS


def test_nested_structures_are_redacted() -> None:
    payload = {
        "request_id": "r-1",
        "metadata": {
            "model_name": "gpt-4o-mini",
            "prompt": "system prompt text",
            "nested": [{"authorization": "Bearer abc"}, {"status": "ok"}],
        },
    }

    flat = json.dumps(redact_fields(payload), ensure_ascii=False)

    assert "gpt-4o-mini" in flat
    assert "system prompt text" not in flat
    assert "Bearer abc" not in flat


def test_raw_content_and_images_never_appear() -> None:
    payload = {
        "request_id": "r-1",
        "message": f"user said something {_BASE64}",
        "memory_content": "victory at the finals",
    }

    flat = json.dumps(redact_fields(payload), ensure_ascii=False)

    assert "user said something" not in flat
    assert "victory at the finals" not in flat
    assert "AAAA" not in flat


def test_redact_text_scrubs_secrets() -> None:
    text = (
        "failed with Authorization: Bearer sk-abcdef123456 "
        "api_key=sk-live-999 dsn=postgresql://user:pw@db:5432/masm "
        f"image={_BASE64}"
    )

    redacted = redact_text(text)

    assert "sk-abcdef123456" not in redacted
    assert "sk-live-999" not in redacted
    assert "postgresql://user:pw@db:5432/masm" not in redacted
    assert "AAAA" not in redacted


def test_redact_text_scrubs_object_storage_keys() -> None:
    text = "upload failed for secret_access_key=AKIA1234567890 and bucket key"

    redacted = redact_text(text)

    assert "AKIA1234567890" not in redacted


def test_exception_text_is_redacted_in_formatted_output() -> None:
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"boom Authorization: Bearer sk-secret-token {_BASE64}")
    except RuntimeError as exc:
        record = logging.LogRecord(
            "masm", logging.ERROR, __file__, 1, "unhandled %s", (exc,), None
        )

    output = formatter.format(record)

    assert "sk-secret-token" not in output
    assert "AAAA" not in output


def test_structured_logging_arguments_are_redacted() -> None:
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "masm",
        logging.INFO,
        __file__,
        1,
        "search done %s",
        ({"request_id": "r-1", "content": "raw memory text"},),
        None,
    )

    output = formatter.format(record)

    assert "r-1" in output
    assert "raw memory text" not in output


def test_log_record_extras_are_redacted() -> None:
    formatter = RedactingFormatter("%(message)s | %(extra)s")
    record = logging.LogRecord("masm", logging.INFO, __file__, 1, "done", (), None)
    record.extra = {"request_id": "r-1", "prompt": "system prompt"}

    output = formatter.format(record)

    assert "r-1" in output
    assert "system prompt" not in output
