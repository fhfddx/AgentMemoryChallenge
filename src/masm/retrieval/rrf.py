"""加权倒数排名融合（Reciprocal Rank Fusion）。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from masm.storage.types import MemoryCandidate


@dataclass(frozen=True)
class RankedChannel:
    """一个召回通道的有序候选（通道内按相关性从高到低）。"""

    name: str
    candidates: Sequence[MemoryCandidate]


@dataclass(frozen=True)
class FusedCandidate:
    """融合后的候选。"""

    memory_id: UUID
    user_id: str
    score: float
    channel_ranks: Mapping[str, int]


def reciprocal_rank_fusion(
    rankings: Sequence[RankedChannel],
    weights: Mapping[str, float],
    k: int = 60,
) -> list[FusedCandidate]:
    """融合多通道排名：``score = Σ weight[channel] / (k + rank)``。

    权重必须由调用方从配置注入，不写死在算法里；权重缺失或不为正的通道不参与融合。
    同一通道内的重复候选只按最佳名次计一次。同分时按 ``memory_id`` 稳定排序。
    """
    if k < 0:
        raise ValueError("k 必须为非负整数")

    scores: dict[UUID, float] = {}
    owners: dict[UUID, str] = {}
    ranks: dict[UUID, dict[str, int]] = {}

    for channel in rankings:
        weight = weights.get(channel.name, 0.0)
        if weight <= 0.0:
            continue
        for position, candidate in enumerate(channel.candidates, start=1):
            per_channel = ranks.setdefault(candidate.memory_id, {})
            if channel.name in per_channel:
                continue
            per_channel[channel.name] = position
            scores[candidate.memory_id] = scores.get(candidate.memory_id, 0.0) + (
                weight / (k + position)
            )
            owners[candidate.memory_id] = candidate.user_id

    fused = [
        FusedCandidate(
            memory_id=memory_id,
            user_id=owners[memory_id],
            score=score,
            channel_ranks=dict(ranks[memory_id]),
        )
        for memory_id, score in scores.items()
    ]
    fused.sort(key=lambda item: (-item.score, str(item.memory_id)))
    return fused
