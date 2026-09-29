"""OpenAI-compatible 文本 Embedding Provider 契约测试。"""

import json
import math

import httpx
import pytest

from masm.providers.errors import ProviderResponseError, ProviderUnavailableError
from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider


def _provider(handler, **overrides: object) -> OpenAICompatibleEmbeddingProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    options: dict[str, object] = {
        "model": "text-embedding-v4",
        "model_version": "text-embedding-v4",
        "dimensions": 2,
        "base_url": "https://embeddings.invalid/v1",
        "api_key": "embedding-secret",
        "client": client,
    }
    options.update(overrides)
    return OpenAICompatibleEmbeddingProvider(**options)


def _success(vectors: list[list[float]]) -> dict:
    return {
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": "text-embedding-v4",
    }


def test_embedding_request_uses_fixed_model_dimensions_and_bearer_token() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=_success([[0.1, 0.2], [0.3, 0.4]]))

    provider = _provider(handler)

    result = provider.embed_texts(["first", "second"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["body"] == {
        "model": "text-embedding-v4",
        "input": ["first", "second"],
        "dimensions": 2,
    }
    assert seen["auth"] == "Bearer embedding-secret"
    assert provider.records[-1].batch_size == 2
    assert provider.records[-1].succeeded is True


def test_embedding_response_is_restored_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
        return httpx.Response(200, json=body)

    assert _provider(handler).embed_texts(["first", "second"]) == [
        [0.1, 0.2],
        [0.3, 0.4],
    ]


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [0.1, 0.2]}],
        [
            {"index": 0, "embedding": [0.1, 0.2]},
            {"index": 0, "embedding": [0.3, 0.4]},
        ],
        [
            {"index": 0, "embedding": [0.1, math.nan]},
            {"index": 1, "embedding": [0.3, 0.4]},
        ],
        [
            {"index": 0, "embedding": [0.1]},
            {"index": 1, "embedding": [0.3, 0.4]},
        ],
    ],
)
def test_embedding_rejects_missing_duplicate_non_finite_and_wrong_dimensions(
    data: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    with pytest.raises(ProviderResponseError):
        _provider(handler, max_attempts=1).embed_texts(["first", "second"])


def test_embedding_retries_transport_failure_at_most_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    provider = _provider(handler, max_attempts=9)

    with pytest.raises(ProviderUnavailableError):
        provider.embed_texts(["first"])

    assert calls == 2
    assert provider.max_attempts == 2
    assert provider.records[-1].attempts == 2
    assert provider.records[-1].succeeded is False


def test_embedding_error_never_contains_secret_or_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": "private-provider-body", "echo": "embedding-secret"},
        )

    with pytest.raises(ProviderUnavailableError) as captured:
        _provider(handler, max_attempts=1).embed_texts(["sensitive input"])

    rendered = str(captured.value)
    assert "embedding-secret" not in rendered
    assert "private-provider-body" not in rendered
    assert "sensitive input" not in rendered


def test_direct_image_embedding_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("直接图片调用不应访问网络")

    with pytest.raises(ProviderResponseError, match="不支持直接图片"):
        _provider(handler).embed_images([b"not-an-image"])
