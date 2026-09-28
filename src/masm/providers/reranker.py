"""相关性 Reranker Provider 接口。

Reranker 只允许产生**相关性分数**：不得返回答案正文、摘要或任何自然语言结论。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

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
