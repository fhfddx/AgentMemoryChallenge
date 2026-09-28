"""结构化模型 Provider 的超时、重试、Schema 校验与版本记录测试。

使用 httpx.MockTransport 注入确定性响应，绝不访问真实模型 API。
"""

import json

import httpx
import pytest

from masm.providers.llm import (
    ModelRequest,
    ModelUnavailableError,
    OpenAICompatibleLLM,
    StructuredOutputError,
)
from masm.schemas.agents import PerceptionResult

_VALID_BODY = {"choices": [{"message": {"content": '{"keywords": ["cat"], "language": "en"}'}}]}
_INVALID_BODY = {"choices": [{"message": {"content": "not json at all"}}]}


def _request(**overrides: object) -> ModelRequest:
    payload: dict = {
        "prompt": "system prompt",
        "payload": {"content": []},
        "model": "gpt-4o-mini",
        "prompt_version": "v1",
    }
    payload.update(overrides)
    return ModelRequest(**payload)


def _llm(handler, **overrides: object) -> OpenAICompatibleLLM:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM(
        model="gpt-4o-mini",
        base_url="https://models.invalid/v1",
        api_key="test-key",
        client=client,
        **overrides,
    )


def test_valid_json_is_validated_and_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    result = _llm(handler).complete_json(_request(), PerceptionResult)

    assert isinstance(result, PerceptionResult)
    assert result.keywords == ("cat",)


def test_json_schema_and_version_are_sent_to_provider() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_VALID_BODY)

    _llm(handler).complete_json(_request(), PerceptionResult)

    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "gpt-4o-mini"
    response_format = seen["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "PerceptionResult"
    assert "properties" in response_format["json_schema"]["schema"]


def test_invalid_json_is_retried_once_then_fails() -> None:
    """一次重试：总计两次调用后仍失败则抛出结构化输出错误。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_INVALID_BODY)

    with pytest.raises(StructuredOutputError):
        _llm(handler).complete_json(_request(), PerceptionResult)

    assert len(calls) == 2


def test_schema_violation_is_retried_and_can_succeed() -> None:
    invalid_schema_body = {"choices": [{"message": {"content": '{"modality": "nope"}'}}]}
    responses = iter(
        [
            httpx.Response(200, json=invalid_schema_body),
            httpx.Response(200, json=_VALID_BODY),
        ]
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    result = _llm(handler).complete_json(_request(), PerceptionResult)

    assert result.keywords == ("cat",)
    assert len(calls) == 2


def test_transport_failure_raises_model_unavailable() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ModelUnavailableError):
        _llm(handler).complete_json(_request(), PerceptionResult)

    assert len(calls) == 2


def test_http_error_status_is_retried_once() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(ModelUnavailableError):
        _llm(handler).complete_json(_request(), PerceptionResult)

    assert len(calls) == 2


def test_request_timeout_is_applied() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=_VALID_BODY)

    _llm(handler, timeout_seconds=7.0).complete_json(_request(), PerceptionResult)

    assert seen["timeout"]["connect"] == 7.0
    assert seen["timeout"]["read"] == 7.0


def test_records_model_prompt_version_and_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    llm = _llm(handler)
    llm.complete_json(_request(), PerceptionResult)

    record = llm.records[-1]
    assert record.model == "gpt-4o-mini"
    assert record.prompt_version == "v1"
    assert record.output_type == "PerceptionResult"
    assert record.attempts == 1
    assert record.succeeded is True


def test_records_failed_attempts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_INVALID_BODY)

    llm = _llm(handler)
    with pytest.raises(StructuredOutputError):
        llm.complete_json(_request(), PerceptionResult)

    record = llm.records[-1]
    assert record.attempts == 2
    assert record.succeeded is False
