"""响应打包：按实际 JSON 序列化字节数裁剪，且只保留完整证据。

保留证据的相对排名不变；单条证据内容永不被截断。超限条目会被跳过，
避免一条过长的上下文遮蔽后续可容纳的短消息证据。
"""

from collections.abc import Sequence

from masm.retrieval.reranker import RankedEvidence
from masm.schemas.api import MemoryEvidence, SearchResponse

# 单次 Search 响应的字节硬上限（30 MiB）。
MAX_RESPONSE_BYTES = 30 * 1024 * 1024


def minimum_response_bytes() -> int:
    """空 SearchResponse 的实际 JSON 序列化字节数。

    任何比它还小的上限都会让响应永远无法表达「零命中」，因此必须被拒绝，而不是静默
    退化成恒定空结果。
    """
    return len(SearchResponse(data=[]).model_dump_json().encode("utf-8"))


def resolve_max_response_bytes(value: int | None = None) -> int:
    """把响应上限约束到 ``minimum_response_bytes()..MAX_RESPONSE_BYTES``。

    非正数或小于最小可序列化响应的值直接拒绝（配置错误应尽早暴露），越界高值安全截断。
    """
    if value is None:
        return MAX_RESPONSE_BYTES
    minimum = minimum_response_bytes()
    if value < minimum:
        raise ValueError(f"max_response_bytes 必须至少为 {minimum} 字节（空响应的序列化大小）")
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
    """按排名顺序打包完整证据，同时遵守 ``top_k`` 与最大响应字节数。"""

    def __init__(self, *, max_bytes: int | None = None) -> None:
        self._max_bytes = resolve_max_response_bytes(max_bytes)

    @property
    def max_bytes(self) -> int:
        """实际生效的最大响应字节数。"""
        return self._max_bytes

    def pack(self, evidence: Sequence[RankedEvidence], top_k: int) -> list[RankedEvidence]:
        """返回不超过 ``top_k`` 且序列化后不超过上限的有序子序列。"""
        if top_k < 1 or not evidence:
            return []
        kept: list[RankedEvidence] = []
        for candidate in evidence:
            if len(kept) >= top_k:
                break
            attempt = [*kept, candidate]
            if serialized_size(attempt) > self._max_bytes:
                # 不裁剪单条证据内容；继续寻找后续可容纳的短证据。
                continue
            kept.append(candidate)
        return kept
