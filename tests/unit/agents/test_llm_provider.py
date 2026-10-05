"""结构化模型 Provider 的超时、重试、Schema 校验与多模态请求体测试。

使用 httpx.MockTransport 注入确定性响应，直接检查最终 HTTP 请求体，绝不访问真实模型 API。
"""

import json
import traceback

import httpx
import pytest

from masm.agents.perception import PerceptionAgent
from masm.providers.errors import ProviderError
from masm.providers.llm import (
    MAX_ATTEMPTS,
    ModelRequest,
    ModelUnavailableError,
    OpenAICompatibleLLM,
    StructuredOutputError,
)
from masm.schemas.agents import PerceptionResult
from masm.schemas.content import ImageURLPart, TextPart

_DATA_URL = "data:image/png;base64,iVBORw0KGgo="
_VALID_BODY = {"choices": [{"message": {"content": '{"keywords": ["cat"], "language": "en"}'}}]}
_INVALID_BODY = {"choices": [{"message": {"content": "not json at all"}}]}


def _text(content: str) -> TextPart:
    return TextPart(text=content)


def _image(url: str = _DATA_URL) -> ImageURLPart:
    return ImageURLPart(image_url={"url": url})


def _request(**overrides: object) -> ModelRequest:
    payload: dict = {
        "prompt": "system prompt",
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


def _recording_handler(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_VALID_BODY)

    return handler


def _user_content(seen: dict):
    return seen["body"]["messages"][1]["content"]


def _schema_nodes(value: object):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _schema_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_nodes(item)


# ---------------------------------------------------------------- 结构化输出


def test_valid_json_is_validated_and_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    result = _llm(handler).complete_json(_request(payload={"content": []}), PerceptionResult)

    assert isinstance(result, PerceptionResult)
    assert result.keywords == ("cat",)


def test_sequential_model_calls_share_owned_client_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串行阶段只创建一个可复用 Client，并在关闭 Provider 时释放它。"""
    clients: list[httpx.Client] = []
    real_client = httpx.Client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    def make_client(*args: object, **kwargs: object) -> httpx.Client:
        client = real_client(*args, transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("masm.providers.llm.httpx.Client", make_client)
    llm = OpenAICompatibleLLM(
        model="gpt-4o-mini", base_url="https://models.invalid/v1", api_key="test-key"
    )
    for _ in range(2):
        assert llm.complete_json(_request(payload={}), PerceptionResult).keywords == ("cat",)

    assert len(clients) == 1
    assert clients[0].is_closed is False
    llm.close()
    assert clients[0].is_closed is True


def test_json_schema_and_version_are_sent_to_provider() -> None:
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(_request(payload={"x": 1}), PerceptionResult)

    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "gpt-4o-mini"
    response_format = seen["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "PerceptionResult"
    assert "properties" in response_format["json_schema"]["schema"]


def test_json_schema_requests_strict_provider_enforcement() -> None:
    """防止模型返回 Schema 之外的字段并触发本地校验失败。"""
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(_request(payload={}), PerceptionResult)

    assert seen["body"]["response_format"]["json_schema"]["strict"] is True


def test_strict_json_schema_requires_every_object_field_and_omits_defaults() -> None:
    """OpenAI 严格输出要求根对象与嵌套对象的全部字段均为 required。"""
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(_request(payload={}), PerceptionResult)

    schema = seen["body"]["response_format"]["json_schema"]["schema"]
    nodes = list(_schema_nodes(schema))
    object_nodes = [node for node in nodes if isinstance(node.get("properties"), dict)]
    assert object_nodes
    for node in object_nodes:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    assert all("default" not in node for node in nodes)


def test_structured_payload_is_sent_as_json_text() -> None:
    """时序智能体等结构化输入仍以 JSON 文本发送，不能变成内容块数组。"""
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(
        _request(payload={"history": [{"memory_id": "m-1", "content": "memory"}]}),
        PerceptionResult,
    )

    content = _user_content(seen)
    assert isinstance(content, str)
    assert json.loads(content) == {"history": [{"memory_id": "m-1", "content": "memory"}]}


# ---------------------------------------------------------------- 多模态请求体


def test_text_only_content_is_sent_as_text_block() -> None:
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(
        _request(content=[_text("a red bicycle")]), PerceptionResult
    )

    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["messages"][0]["content"] == "system prompt"
    assert _user_content(seen) == [{"type": "text", "text": "a red bicycle"}]


def test_image_only_content_is_sent_as_image_block() -> None:
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(
        _request(content=[_image()]), PerceptionResult
    )

    assert _user_content(seen) == [{"type": "image_url", "image_url": {"url": _DATA_URL}}]


def test_mixed_content_preserves_block_order() -> None:
    seen: dict = {}

    _llm(_recording_handler(seen)).complete_json(
        _request(content=[_text("first"), _image(), _text("second")]), PerceptionResult
    )

    blocks = _user_content(seen)
    assert [block["type"] for block in blocks] == ["text", "image_url", "text"]
    assert blocks[0]["text"] == "first"
    assert blocks[1]["image_url"]["url"] == _DATA_URL
    assert blocks[2]["text"] == "second"


def test_perception_agent_sends_multimodal_blocks_over_http() -> None:
    """端到端：感知智能体产生的最终 HTTP 请求体必须携带图片内容块。"""
    seen: dict = {}
    agent = PerceptionAgent(_llm(_recording_handler(seen)))

    agent.extract([_text("before"), _image(), _text("after")])

    blocks = _user_content(seen)
    assert [block["type"] for block in blocks] == ["text", "image_url", "text"]
    assert blocks[1]["image_url"]["url"] == _DATA_URL
    assert seen["body"]["messages"][0]["role"] == "system"


# ---------------------------------------------------------------- 重试与超时


def test_invalid_json_is_retried_once_then_fails() -> None:
    """一次重试：总计两次调用后仍失败则抛出结构化输出错误。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_INVALID_BODY)

    with pytest.raises(StructuredOutputError):
        _llm(handler).complete_json(_request(payload={}), PerceptionResult)

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

    result = _llm(handler).complete_json(_request(payload={}), PerceptionResult)

    assert result.keywords == ("cat",)
    assert len(calls) == 2


def test_transport_failure_raises_model_unavailable() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ModelUnavailableError):
        _llm(handler).complete_json(_request(payload={}), PerceptionResult)

    assert len(calls) == 2


def test_http_error_status_is_retried_once() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(ModelUnavailableError):
        _llm(handler).complete_json(_request(payload={}), PerceptionResult)

    assert len(calls) == 2


def test_llm_provider_errors_share_sanitized_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "private-provider-body"})

    with pytest.raises(ProviderError) as captured:
        _llm(handler, max_attempts=1).complete_json(_request(payload={}), PerceptionResult)

    assert "private-provider-body" not in str(captured.value)


def test_llm_invalid_payload_is_absent_from_full_traceback() -> None:
    """完整异常链不得保留供应商返回的无效结构化内容。"""
    secret_value = "private-llm-response-field"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": secret_value}}]},
        )

    llm = _llm(handler, max_attempts=1)
    with pytest.raises(StructuredOutputError) as captured:
        llm.complete_json(_request(payload={}), PerceptionResult)

    rendered = "".join(traceback.format_exception(captured.value))
    assert secret_value not in rendered


def test_request_timeout_is_applied() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=_VALID_BODY)

    _llm(handler, timeout_seconds=7.0).complete_json(_request(payload={}), PerceptionResult)

    assert seen["timeout"]["connect"] == 7.0
    assert seen["timeout"]["read"] == 7.0


# ---------------------------------------------------------------- 重试上限


def test_provider_attempt_cap_is_enforced() -> None:
    """Provider 级配置突破上限时安全截断，总调用次数最多为 MAX_ATTEMPTS。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_INVALID_BODY)

    llm = _llm(handler, max_attempts=5)

    assert llm.max_attempts == MAX_ATTEMPTS
    with pytest.raises(StructuredOutputError):
        llm.complete_json(_request(payload={}), PerceptionResult)
    assert len(calls) == MAX_ATTEMPTS


def test_request_attempt_cap_is_enforced() -> None:
    """请求级配置同样不能突破上限。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_INVALID_BODY)

    llm = _llm(handler, max_attempts=MAX_ATTEMPTS)

    with pytest.raises(StructuredOutputError):
        llm.complete_json(_request(payload={}, max_attempts=9), PerceptionResult)
    assert len(calls) == MAX_ATTEMPTS


@pytest.mark.parametrize("attempts", [0, -1])
def test_non_positive_provider_attempts_are_rejected(attempts: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    with pytest.raises(ValueError):
        _llm(handler, max_attempts=attempts)


@pytest.mark.parametrize("attempts", [0, -3])
def test_non_positive_request_attempts_are_rejected(attempts: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_VALID_BODY)

    llm = _llm(handler)

    with pytest.raises(ValueError):
        llm.complete_json(_request(payload={}, max_attempts=attempts), PerceptionResult)
    assert calls == []


def test_single_attempt_is_allowed() -> None:
    """保留只调用一次、不重试的能力。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_INVALID_BODY)

    llm = _llm(handler, max_attempts=1)

    assert llm.max_attempts == 1
    with pytest.raises(StructuredOutputError):
        llm.complete_json(_request(payload={}), PerceptionResult)
    assert len(calls) == 1


# ---------------------------------------------------------------- 调用记录


def test_records_model_prompt_version_and_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_VALID_BODY)

    llm = _llm(handler)
    llm.complete_json(_request(payload={}), PerceptionResult)

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
        llm.complete_json(_request(payload={}), PerceptionResult)

    record = llm.records[-1]
    assert record.attempts == MAX_ATTEMPTS
    assert record.succeeded is False
