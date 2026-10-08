"""基于召回通道原始信号的强锚点门控。"""

import math
from collections.abc import Sequence

from masm.retrieval.baseline import (
    IMAGE_VECTOR_CHANNEL,
    LEXICAL_CHANNEL,
    TEXT_VECTOR_CHANNEL,
)
from masm.storage.types import MemoryCandidate

DEFAULT_MIN_TEXT_SIMILARITY = 0.48
DEFAULT_MIN_IMAGE_SIMILARITY = 0.42


def _validate_similarity(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or not -1.0 <= result <= 1.0:
        raise ValueError(f"{name} 必须是 [-1, 1] 内的有限余弦相似度")
    return result


class RelevanceGate:
    """只保留具有全文命中或足够强向量相似度的初始召回候选。"""

    def __init__(
        self,
        *,
        min_text_similarity: float = DEFAULT_MIN_TEXT_SIMILARITY,
        min_image_similarity: float = DEFAULT_MIN_IMAGE_SIMILARITY,
    ) -> None:
        self._min_text_similarity = _validate_similarity(
            "min_text_similarity", min_text_similarity
        )
        self._min_image_similarity = _validate_similarity(
            "min_image_similarity", min_image_similarity
        )

    @property
    def min_text_similarity(self) -> float:
        return self._min_text_similarity

    @property
    def min_image_similarity(self) -> float:
        return self._min_image_similarity

    def filter(self, candidates: Sequence[MemoryCandidate]) -> list[MemoryCandidate]:
        """保持输入顺序，移除没有任何强召回信号的候选。"""
        return [candidate for candidate in candidates if self._is_strong(candidate)]

    def _is_strong(self, candidate: MemoryCandidate) -> bool:
        signals = candidate.retrieval_signals
        if LEXICAL_CHANNEL in signals:
            return True
        if signals.get(TEXT_VECTOR_CHANNEL, -1.0) >= self._min_text_similarity:
            return True
        return signals.get(IMAGE_VECTOR_CHANNEL, -1.0) >= self._min_image_similarity
