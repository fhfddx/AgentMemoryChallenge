"""HttpReranker：只产出相关性分数，任何异常都让调用方退回规则排序。"""

import httpx
import pytest

from masm.providers.reranker import HttpReranker, RerankProviderError


def _reranker(handler: object) -> HttpReranker:
    return HttpReranker(
        base_url="https://example.test",
        api_key="secret-key",
        model="qwen3-rerank",
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )


def test_scores_are_aligned_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                }
            },
        )

    assert _reranker(handler).score("q", ["a", "b"]) == [0.1, 0.9]


def test_missing_indices_score_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"output": {"results": [{"index": 0, "relevance_score": 0.4}]}}
        )

    assert _reranker(handler).score("q", ["a", "b", "c"]) == [0.4, 0.0, 0.0]


def test_empty_documents_skip_the_call() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"output": {"results": []}})

    assert _reranker(handler).score("q", []) == []
    assert calls == []


def test_http_error_raises_for_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": "Throttled"})

    with pytest.raises(RerankProviderError):
        _reranker(handler).score("q", ["a"])


def test_malformed_payload_raises_for_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"results": "nope"}})

    with pytest.raises(RerankProviderError):
        _reranker(handler).score("q", ["a"])


def test_bad_index_or_score_raises() -> None:
    def bad_index(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"output": {"results": [{"index": 5, "relevance_score": 0.5}]}}
        )

    def bad_score(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"output": {"results": [{"index": 0, "relevance_score": "high"}]}}
        )

    with pytest.raises(RerankProviderError):
        _reranker(bad_index).score("q", ["a"])
    with pytest.raises(RerankProviderError):
        _reranker(bad_score).score("q", ["a"])


def test_network_failure_raises_for_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RerankProviderError):
        _reranker(handler).score("q", ["a"])


def test_constructor_rejects_partial_configuration() -> None:
    with pytest.raises(ValueError):
        HttpReranker(base_url="https://example.test", api_key="", model="qwen3-rerank")
    with pytest.raises(ValueError):
        HttpReranker(base_url="", api_key="k", model="qwen3-rerank")
