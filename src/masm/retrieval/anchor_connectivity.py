"""以问题锚点为根的有界关系可达性判定。

只读取已持久化的同用户记忆关系。治理关系边只挂在 context 节点上，而 selector 池里
常常是 message 证据，所以先把每条锚点证据映射到同源 context 节点，再做至多
``max_hops`` 跳的广度优先扩展。全部读取都在 SQL 阶段按 ``user_id`` 过滤，因此该判定
绝不跨用户泄漏或联通任何记忆。

深度取舍：``DEFAULT_ANCHOR_HOPS`` 与检索侧的有界两跳预算一致，所以「扩展本来就能取到
的链路」一定被判为同分量；把深度调大只会让判定更宽松（更接近加校验之前的行为），调小
则更严格。判定的作用不是限制召回，而是把「与锚点毫无关系边」的证据从混合选择里剔除。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from masm.storage.types import MemoryCandidate

# 单跳读取上限与硬上限：可达性判定不会因为关系图拥挤而放大读取量。
DEFAULT_HOP_LIMIT = 64
MAX_HOP_LIMIT = 256
DEFAULT_ANCHOR_HOPS = 2
MAX_ANCHOR_HOPS = 4


class RelationLookup(Protocol):
    """可达性判定所需的最小 repository 能力。"""

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> Sequence[MemoryCandidate]: ...

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> Sequence[MemoryCandidate]: ...


@dataclass(frozen=True)
class ConnectedEvidence:
    """与锚点同分量的证据范围：命中的记忆 ID 以及命中的运行（request_id）。"""

    memory_ids: frozenset[UUID]
    request_ids: frozenset[str]


class AnchorConnectivity:
    """有界、同用户、以锚点为根的关系可达性。"""

    def __init__(
        self, repository: RelationLookup, *, hop_limit: int = DEFAULT_HOP_LIMIT
    ) -> None:
        if not 1 <= hop_limit <= MAX_HOP_LIMIT:
            raise ValueError(f"hop_limit 必须在 1..{MAX_HOP_LIMIT} 之间")
        self._repository = repository
        self._hop_limit = hop_limit

    @property
    def hop_limit(self) -> int:
        """单跳读取上限。"""
        return self._hop_limit

    def connected(
        self,
        user_id: str,
        memory_ids: Sequence[UUID],
        request_ids: Sequence[str],
        max_hops: int,
    ) -> ConnectedEvidence:
        """返回锚点记忆、它们的同源 context，以及至多 ``max_hops`` 跳内可达的范围。

        命中的运行会被整体记为可达：同一 request_id 下的 message 与 context 是同一份
        来源证据，这与 Search 侧按 request_id 回填消息证据的既有语义一致。
        """
        if not 0 <= max_hops <= MAX_ANCHOR_HOPS:
            raise ValueError(f"max_hops 必须在 0..{MAX_ANCHOR_HOPS} 之间")
        visited_ids: set[UUID] = set(memory_ids)
        visited_runs: set[str] = {run for run in request_ids if run}
        frontier: list[UUID] = list(visited_ids)

        runs = [run for run in dict.fromkeys(request_ids) if run]
        parents = (
            self._repository.context_candidates_for_requests(user_id, runs) if runs else []
        )
        for parent in parents:
            if parent.request_id:
                visited_runs.add(parent.request_id)
            if parent.memory_id in visited_ids:
                continue
            visited_ids.add(parent.memory_id)
            frontier.append(parent.memory_id)

        for _ in range(max_hops):
            if not frontier:
                break
            neighbours = self._repository.related(user_id, frontier, self._hop_limit)
            frontier = []
            for candidate in neighbours:
                if candidate.request_id:
                    visited_runs.add(candidate.request_id)
                if candidate.memory_id in visited_ids:
                    continue
                visited_ids.add(candidate.memory_id)
                frontier.append(candidate.memory_id)

        return ConnectedEvidence(frozenset(visited_ids), frozenset(visited_runs))
