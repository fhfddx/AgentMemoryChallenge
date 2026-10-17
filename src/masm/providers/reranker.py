"""相关性 Reranker Provider 接口。

Reranker 只允许产生**相关性分数**：不得返回答案正文、摘要或任何自然语言结论。
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

import httpx

# 参与重排的候选硬上限。
MAX_RERANK_INPUT = 64


def resolve_rerank_input(value: int | None = None) -> int:
    """把重排候选上限约束到 ``0..MAX_RERANK_INPUT``：负数拒绝，越界安全截断。"""
    if value is None:
        return MAX_RERANK_INPUT
    if value < 0:
        raise ValueError("max_candidates 必须为非负整数")
    return min(value, MAX_RERANK_INPUT)


class RerankerProvider(ABC):
    """相关性打分 Provider：输入查询与文档，输出等长的分数列表。"""

    model_name: str

    @abstractmethod
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """返回与 ``documents`` 等长的相关性分数（不做任何文本生成）。"""


class LexicalReranker(RerankerProvider):
    """确定性的词重叠打分器：无外部依赖，用于基线与本地开发。"""

    model_name = "lexical-overlap"

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return [0.0 for _ in documents]
        scores: list[float] = []
        for document in documents:
            document_tokens = _tokens(document)
            if not document_tokens:
                scores.append(0.0)
                continue
            overlap = len(query_tokens & document_tokens)
            scores.append(overlap / len(query_tokens))
        return scores


def _tokens(text: str) -> set[str]:
    return {token for token in text.lower().split() if token}


class RerankProviderError(RuntimeError):
    """重排 Provider 失败；调用方退回规则排序，绝不因此让搜索失败。"""


class HttpReranker(RerankerProvider):
    """DashScope 兼容的 Qwen3-Rerank Provider（官方规则允许自由选择 Reranker）。

    只输出相关性分数，不生成任何文本。网络、协议或结构异常一律抛
    ``RerankProviderError``，由 ``EvidenceReranker`` 退回纯规则排序。
    """

    DEFAULT_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
    MAX_DOCUMENT_CHARS = 2000

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        path: str = DEFAULT_PATH,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url or not api_key or not model:
            raise ValueError("rerank base_url、api_key 与 model 均为必填")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("rerank timeout_seconds 必须为有限正数")
        self.model_name = model
        self._url = base_url.rstrip("/") + path
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._client = client

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        payload = {
            "model": self.model_name,
            "input": {
                "query": query[: self.MAX_DOCUMENT_CHARS],
                "documents": [
                    document[: self.MAX_DOCUMENT_CHARS] for document in documents
                ],
            },
            "parameters": {"return_documents": False, "top_n": len(documents)},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is None:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(self._url, json=payload, headers=headers)
            else:
                response = self._client.post(
                    self._url, json=payload, headers=headers, timeout=self._timeout
                )
        except Exception as exc:
            raise RerankProviderError("rerank request failed") from exc
        if response.status_code != 200:
            raise RerankProviderError(f"rerank http status {response.status_code}")
        try:
            results = response.json()["output"]["results"]
        except Exception as exc:
            raise RerankProviderError("rerank payload invalid") from exc
        if not isinstance(results, list):
            raise RerankProviderError("rerank payload invalid")
        scores = [0.0] * len(documents)
        seen = 0
        for item in results:
            if not isinstance(item, dict):
                raise RerankProviderError("rerank result invalid")
            index = item.get("index")
            value = item.get("relevance_score")
            if type(index) is not int or not 0 <= index < len(documents):
                raise RerankProviderError("rerank index invalid")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise RerankProviderError("rerank score invalid")
            scores[index] = float(value)
            seen += 1
        if seen == 0:
            raise RerankProviderError("rerank returned no scores")
        return scores
