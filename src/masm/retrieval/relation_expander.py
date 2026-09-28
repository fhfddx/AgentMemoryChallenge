"""一跳关系扩展与冲突补全。

只扩展一跳：绝不对扩展结果再次调用关系查询。全部关系读取都在 SQL 阶段按 user_id
过滤，因此即使数据库中异常存在跨用户关系边，也不会泄漏另一用户的记忆。
"""

from collections.abc import Sequence
from dataclasses import replace

from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate

# 种子数量与扩展结果数量的硬上限。
MAX_SEEDS = 8
MAX_EXPANDED = 32


def resolve_cap(value: int | None, hard_cap: int, name: str) -> int:
    """把数量上限约束到 ``0..hard_cap``：负数拒绝，越界安全截断。"""
    if value is None:
        return hard_cap
    if value < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return min(value, hard_cap)


class RelationExpander:
    """一跳关系扩展器（含冲突组补全）。"""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        max_seeds: int | None = None,
        max_expanded: int | None = None,
    ) -> None:
        self._repo = repository
        self._max_seeds = resolve_cap(max_seeds, MAX_SEEDS, "max_seeds")
        self._max_expanded = resolve_cap(max_expanded, MAX_EXPANDED, "max_expanded")

    @property
    def max_seeds(self) -> int:
        """参与扩展的种子硬上限。"""
        return self._max_seeds

    @property
    def max_expanded(self) -> int:
        """扩展结果硬上限。"""
        return self._max_expanded

    def expand(
        self, user_id: str, seeds: Sequence[MemoryCandidate], limit: int
    ) -> list[MemoryCandidate]:
        """返回一跳邻居与冲突组同伴，绝不返回二跳节点。"""
        bounded_seeds = list(seeds)[: self._max_seeds]
        if not bounded_seeds:
            return []
        budget = min(limit, self._max_expanded)
        if budget < 1:
            return []

        seed_ids = [seed.memory_id for seed in bounded_seeds]
        related = self._repo.related(user_id, seed_ids, budget)
        peers = self._complete_conflicts(user_id, bounded_seeds, budget)

        results: list[MemoryCandidate] = []
        seen = set(seed_ids)
        for candidate in [*related, *peers]:
            if candidate.memory_id in seen:
                continue
            seen.add(candidate.memory_id)
            results.append(candidate)
            if len(results) >= budget:
                break
        return results

    def _complete_conflicts(
        self, user_id: str, seeds: Sequence[MemoryCandidate], budget: int
    ) -> list[MemoryCandidate]:
        """冲突组命中一侧时补全另一侧，并保留冲突身份。"""
        grouped = [seed for seed in seeds if seed.conflict_group_id is not None]
        if not grouped:
            return []
        distinct_groups = {seed.conflict_group_id for seed in grouped}
        fallback_group = next(iter(distinct_groups)) if len(distinct_groups) == 1 else None
        peers = self._repo.conflict_peers(user_id, [seed.memory_id for seed in grouped], budget)
        completed: list[MemoryCandidate] = []
        for peer in peers:
            if peer.conflict_group_id is None and fallback_group is not None:
                # 补全的同伴必须保留冲突身份，否则重排无法识别冲突对。
                peer = replace(peer, conflict_group_id=fallback_group)
            completed.append(peer)
        return completed
