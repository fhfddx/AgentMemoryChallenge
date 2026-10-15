"""有界二跳关系扩展与冲突补全。

只沿已持久化的记忆关系扩展至多两跳。全部关系读取都在 SQL 阶段按 user_id 过滤，
因此即使数据库中异常存在跨用户关系边，也不会泄漏另一用户的记忆。
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate, RelationEdge

# 种子数量与扩展结果数量的硬上限。
MAX_SEEDS = 8
MAX_EXPANDED = 32
MAX_SECOND_HOP = 8
# 溯源 sidecar 的边数量上限。
MAX_PROVENANCE_EDGES = 64


@dataclass(frozen=True)
class ExpansionEdge:
    """一条真实驱动扩展的已持久化关系边（供证据图溯源校验）。"""

    predecessor_id: UUID
    candidate_id: UUID
    relation_type: str
    depth: int


@dataclass(frozen=True)
class ExpansionResult:
    """扩展结果：候选列表保持不变，另附有界溯源边 sidecar。"""

    candidates: tuple[MemoryCandidate, ...]
    edges: tuple[ExpansionEdge, ...]


def resolve_cap(value: int | None, hard_cap: int, name: str) -> int:
    """把数量上限约束到 ``0..hard_cap``：负数拒绝，越界安全截断。"""
    if value is None:
        return hard_cap
    if value < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return min(value, hard_cap)


class RelationExpander:
    """有界二跳关系扩展器（含冲突组补全）。"""

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
        """返回冲突同伴与至多二跳的显式关系邻居。"""
        return list(self._expand(user_id, seeds, limit, collect_edges=False).candidates)

    def expand_with_provenance(
        self, user_id: str, seeds: Sequence[MemoryCandidate], limit: int
    ) -> ExpansionResult:
        """与 :meth:`expand` 完全相同的候选、顺序与上限，另附有界溯源 sidecar。

        额外的只读关系边查询只在调用本方法时发生；sidecar 绝不参与排序或预算，
        也绝不写入日志或公共响应。
        """
        return self._expand(user_id, seeds, limit, collect_edges=True)

    def _expand(
        self,
        user_id: str,
        seeds: Sequence[MemoryCandidate],
        limit: int,
        *,
        collect_edges: bool,
    ) -> ExpansionResult:
        if self._max_seeds < 1:
            return ExpansionResult((), ())
        bounded_seeds: list[MemoryCandidate] = []
        seed_ids_seen: set[UUID] = set()
        for seed in seeds:
            if seed.memory_id in seed_ids_seen:
                continue
            seed_ids_seen.add(seed.memory_id)
            bounded_seeds.append(seed)
            if len(bounded_seeds) >= self._max_seeds:
                break
        if not bounded_seeds:
            return ExpansionResult((), ())
        budget = min(limit, self._max_expanded)
        if budget < 1:
            return ExpansionResult((), ())

        seed_ids = [seed.memory_id for seed in bounded_seeds]
        edges: list[ExpansionEdge] = []
        # 冲突补全优先占用预算，剩余预算才给普通关系邻居。
        peers = self._complete_conflicts(user_id, bounded_seeds, budget)
        results: list[MemoryCandidate] = []
        seen = set(seed_ids)
        for candidate in sorted(
            peers, key=lambda item: (str(item.conflict_group_id), str(item.memory_id))
        ):
            if candidate.memory_id in seen:
                continue
            seen.add(candidate.memory_id)
            results.append(candidate)
            if len(results) >= budget:
                return ExpansionResult(tuple(results), ())

        relation_budget = budget - len(results)
        if relation_budget < 1:
            return ExpansionResult(tuple(results), ())

        direct_hop = _ordered_unique(
            self._repo.related(user_id, seed_ids, budget),
            excluded=seed_ids,
        )
        if not direct_hop:
            return ExpansionResult(tuple(results), ())
        if collect_edges:
            edges.extend(
                _provenance_edges(
                    self._repo.relation_edges(user_id, seed_ids, MAX_PROVENANCE_EDGES),
                    predecessors=set(seed_ids),
                    candidates={item.memory_id for item in direct_hop},
                    depth=1,
                )
            )
        direct_ids = {candidate.memory_id for candidate in direct_hop}
        bridge_already_emitted = any(
            candidate.memory_id in direct_ids for candidate in results
        )
        first_hop = [
            candidate for candidate in direct_hop if candidate.memory_id not in seen
        ]
        required_first_slots = 0 if bridge_already_emitted else 1

        # 若输出中还没有桥节点，最后一个槽位必须留给直接一跳，不能被端点挤掉。
        if relation_budget <= required_first_slots:
            results.append(first_hop[0])
            return ExpansionResult(tuple(results), _bounded_edges(edges))

        traversal_seeds = direct_hop[: self._max_seeds]
        second_hop = _ordered_unique(
            self._repo.related(
                user_id,
                [candidate.memory_id for candidate in traversal_seeds],
                min(MAX_SECOND_HOP, relation_budget - required_first_slots),
            ),
            excluded={*seen, *(candidate.memory_id for candidate in direct_hop)},
        )
        if collect_edges:
            edges.extend(
                _provenance_edges(
                    self._repo.relation_edges(
                        user_id,
                        [candidate.memory_id for candidate in traversal_seeds],
                        MAX_PROVENANCE_EDGES,
                    ),
                    predecessors={
                        candidate.memory_id for candidate in traversal_seeds
                    },
                    candidates={item.memory_id for item in direct_hop}
                    | {item.memory_id for item in second_hop},
                    depth=2,
                )
            )

        # 为第二跳预留至多八个槽位，但始终至少保留一个直接桥节点。
        second_quota = min(
            len(second_hop), MAX_SECOND_HOP, relation_budget - required_first_slots
        )
        first_quota = min(len(first_hop), relation_budget - second_quota)
        unused = relation_budget - first_quota - second_quota
        if unused:
            second_quota += min(unused, len(second_hop) - second_quota)

        results.extend(first_hop[:first_quota])
        results.extend(second_hop[:second_quota])
        return ExpansionResult(tuple(results), _bounded_edges(edges))

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


def _ordered_unique(
    candidates: Sequence[MemoryCandidate], *, excluded: Collection[UUID]
) -> list[MemoryCandidate]:
    """按分数和 ID 稳定排序，并过滤已见节点与重复路径。"""
    seen = set(excluded)
    ordered: list[MemoryCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, str(item.memory_id))):
        if candidate.memory_id in seen:
            continue
        seen.add(candidate.memory_id)
        ordered.append(candidate)
    return ordered


def _provenance_edges(
    rows: Sequence[RelationEdge],
    *,
    predecessors: Collection[UUID],
    candidates: Collection[UUID],
    depth: int,
) -> list[ExpansionEdge]:
    """把真实关系行转换成有界 sidecar 边；两端都必须落在本次查询覆盖到的集合内。"""
    allowed = set(predecessors) | set(candidates)
    edges: list[ExpansionEdge] = []
    seen: set[tuple[UUID, UUID, str, int]] = set()
    for row in rows:
        if row.source_id in predecessors:
            predecessor, candidate = row.source_id, row.target_id
        elif row.target_id in predecessors:
            predecessor, candidate = row.target_id, row.source_id
        else:
            continue
        if candidate not in allowed:
            continue
        key = (predecessor, candidate, row.relation_type, depth)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            ExpansionEdge(
                predecessor_id=predecessor,
                candidate_id=candidate,
                relation_type=row.relation_type,
                depth=depth,
            )
        )
        if len(edges) >= MAX_PROVENANCE_EDGES:
            break
    return edges


def _bounded_edges(edges: Sequence[ExpansionEdge]) -> tuple[ExpansionEdge, ...]:
    return tuple(edges[:MAX_PROVENANCE_EDGES])
