"""日志脱敏单元测试：字段白名单 + 自由文本脱敏，覆盖嵌套结构、异常与 logging 参数。"""

import json
import logging
import sys

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

    # 未列入白名单的键（含容器）连同其全部内容被整体丢弃。
    assert "gpt-4o-mini" not in flat
    assert "system prompt text" not in flat
    assert "Bearer abc" not in flat
    assert "r-1" in flat


def test_token_usage_keeps_only_numeric_statistics() -> None:
    """token_usage 只能是受控的数值统计，不能成为任意容器的透传通道。"""
    valid = {
        "token_usage": {
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "total_tokens": 46,
            "cached_tokens": 0,
        }
    }
    assert redact_fields(valid)["token_usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
        "cached_tokens": 0,
    }

    poisoned = {
        "token_usage": {
            "total_tokens": 46,
            "messages": ["secret user text"],
            "prompt": "system prompt",
            "raw_response": {"completion": "leaked model output"},
        }
    }
    redacted = redact_fields(poisoned)

    # 出现任何非受控键即整体丢弃，绝不逐键挑拣后保留容器。
    assert "token_usage" not in redacted
    assert "secret user text" not in json.dumps(redacted, ensure_ascii=False)
    assert "leaked model output" not in json.dumps(redacted, ensure_ascii=False)


def test_token_usage_rejects_non_numeric_values() -> None:
    """受控键里的非数值取值同样必须被拒绝。"""
    redacted = redact_fields({"token_usage": {"total_tokens": "secret user text"}})

    assert "token_usage" not in redacted


def test_failure_accepts_only_exception_types_and_error_codes() -> None:
    """failure 只接受异常类型名或内部错误码；自由文本一律替换掉。"""
    assert redact_fields({"failure": "TimeoutError"})["failure"] == "TimeoutError"
    assert redact_fields({"failure": "E_UPSTREAM_TIMEOUT"})["failure"] == "E_UPSTREAM_TIMEOUT"

    leaked = redact_fields({"failure": "private body of user message"})
    assert leaked["failure"] != "private body of user message"
    assert "private body" not in json.dumps(leaked, ensure_ascii=False)


def test_nested_containers_for_whitelisted_scalar_fields_are_dropped() -> None:
    """白名单里的标量字段一旦被塞进容器，就必须被拒绝而不是原样输出。"""
    payload = {
        "request_id": {"nested": "secret"},
        "model_name": ["secret"],
        "status": {"private": "body"},
        "latency_ms": {"private": "body"},
    }

    redacted = redact_fields(payload)

    assert redacted == {}


def test_formatter_drops_container_leaks() -> None:
    """端到端：格式化输出里不得出现经容器透传的私有内容。"""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "masm",
        logging.INFO,
        __file__,
        1,
        "model call %s",
        (
            {
                "event": "model.call",
                "token_usage": {"total_tokens": 5, "messages": ["private body"]},
                "failure": "private body",
            },
        ),
        None,
    )

    output = formatter.format(record)

    assert "private body" not in output
    assert "messages" not in output


def test_formatter_keeps_valid_token_statistics() -> None:
    """受控的 token 统计仍必须进入日志（修复不能把可观测性一并砍掉）。"""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "masm",
        logging.INFO,
        __file__,
        1,
        "model call %s",
        ({"event": "model.call", "token_usage": {"total_tokens": 5}},),
        None,
    )

    output = formatter.format(record)

    assert '"total_tokens": 5' in output


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
    except RuntimeError:
        # 与真实 logging 调用一致：exc_info 由 sys.exc_info() 填充。
        record = logging.LogRecord(
            "masm", logging.ERROR, __file__, 1, "unhandled", (), sys.exc_info()
        )

    output = formatter.format(record)

    assert "sk-secret-token" not in output
    assert "AAAA" not in output
    # 异常类型本身仍需保留（可观测性不能因脱敏而丢失）。
    assert "RuntimeError" in output


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
