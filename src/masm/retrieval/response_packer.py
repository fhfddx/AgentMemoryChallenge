"""响应打包：按实际 JSON 序列化字节数裁剪，且只保留完整证据。

裁剪结果始终是原排序结果的**严格前缀**；单条证据内容永不被截断；若第一条证据
本身就超过上限，则返回空列表而不是半条证据。
"""

from collections.abc import Sequence

from masm.retrieval.reranker import RankedEvidence
from masm.schemas.api import MemoryEvidence, SearchResponse

# 单次 Search 响应的字节硬上限（30 MiB）。
MAX_RESPONSE_BYTES = 30 * 1024 * 1024


def resolve_max_response_bytes(value: int | None = None) -> int:
    """把响应上限约束到 ``0..MAX_RESPONSE_BYTES``：负数拒绝，越界安全截断。"""
    if value is None:
        return MAX_RESPONSE_BYTES
    if value < 0:
        raise ValueError("max_response_bytes 必须为非负整数")
    return min(value, MAX_RESPONSE_BYTES)


def serialized_size(evidence: Sequence[RankedEvidence]) -> int:
    """返回这些证据打包成官方响应后的实际 JSON 序列化字节数。"""
    payload = SearchResponse(
        data=[
            MemoryEvidence(id=str(item.memory_id), content=item.content, score=item.score)
            for item in evidence
        ]
    )
    return len(payload.model_dump_json().encode("utf-8"))


class ResponsePacker:
    """按排名前缀打包证据，同时遵守 ``top_k`` 与最大响应字节数。"""

    def __init__(self, *, max_bytes: int | None = None) -> None:
        self._max_bytes = resolve_max_response_bytes(max_bytes)

    @property
    def max_bytes(self) -> int:
        """实际生效的最大响应字节数。"""
        return self._max_bytes

    def pack(self, evidence: Sequence[RankedEvidence], top_k: int) -> list[RankedEvidence]:
        """返回不超过 ``top_k`` 且序列化后不超过上限的排名前缀。"""
        if top_k < 1 or not evidence:
            return []
        kept: list[RankedEvidence] = []
        for candidate in evidence:
            if len(kept) >= top_k:
                break
            attempt = [*kept, candidate]
            if serialized_size(attempt) > self._max_bytes:
                # 不裁剪单条证据内容；直接停止，保证结果是严格前缀。
                break
            kept.append(candidate)
        return kept
