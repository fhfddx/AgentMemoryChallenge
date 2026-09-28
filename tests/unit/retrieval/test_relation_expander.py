"""一跳关系扩展单元测试（不得扩展到二跳，数量必须有硬上限）。"""

from uuid import UUID, uuid4

import pytest

from masm.retrieval.relation_expander import (
    MAX_EXPANDED,
    MAX_SEEDS,
    RelationExpander,
)
from masm.storage.types import MemoryCandidate

_USER = "user-1"


def _candidate(memory_id: UUID, content: str = "memory") -> MemoryCandidate:
    return MemoryCandidate(memory_id=memory_id, user_id=_USER, content=content, score=0.0)


class _GraphRepository:
    """内存图仓库：adjacency 表示一跳邻居，conflict_members 表示冲突组。"""

    def __init__(self, adjacency: dict[UUID, list[UUID]] | None = None) -> None:
        self.adjacency = adjacency or {}
        self.conflict_members: dict[UUID, list[UUID]] = {}
        self.related_calls: list[list[UUID]] = []
        self.conflict_calls: list[list[UUID]] = []

    def related(self, user_id: str, memory_ids, limit: int) -> list[MemoryCandidate]:
        assert user_id == _USER
        seeds = list(memory_ids)
        self.related_calls.append(seeds)
        found: list[UUID] = []
        for seed in seeds:
            found.extend(self.adjacency.get(seed, []))
        unique = [memory_id for memory_id in dict.fromkeys(found) if memory_id not in set(seeds)]
        return [_candidate(memory_id) for memory_id in unique[:limit]]

    def conflict_peers(self, user_id: str, memory_ids, limit: int) -> list[MemoryCandidate]:
        assert user_id == _USER
        seeds = list(memory_ids)
        self.conflict_calls.append(seeds)
        found: list[UUID] = []
        for seed in seeds:
            found.extend(self.conflict_members.get(seed, []))
        unique = [memory_id for memory_id in dict.fromkeys(found) if memory_id not in set(seeds)]
        return [_candidate(memory_id) for memory_id in unique[:limit]]


def test_expands_exactly_one_hop() -> None:
    """A-B-C 链：以 A 为种子只能拿到 B，二跳节点 C 不得出现。"""
    a, b, c = uuid4(), uuid4(), uuid4()
    repository = _GraphRepository({a: [b], b: [c]})
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [_candidate(a)], 10)

    assert [candidate.memory_id for candidate in expanded] == [b]
    assert len(repository.related_calls) == 1, "只允许一次一跳查询"


def test_seeds_are_not_returned_as_expansion() -> None:
    a, b = uuid4(), uuid4()
    repository = _GraphRepository({a: [b, a]})
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [_candidate(a)], 10)

    assert [candidate.memory_id for candidate in expanded] == [b]


def test_seed_count_is_capped() -> None:
    seeds = [_candidate(uuid4()) for _ in range(MAX_SEEDS + 5)]
    repository = _GraphRepository()
    expander = RelationExpander(repository)

    expander.expand(_USER, seeds, 10)

    assert len(repository.related_calls[0]) == MAX_SEEDS


def test_expansion_is_capped_by_hard_limit() -> None:
    a = uuid4()
    neighbours = [uuid4() for _ in range(MAX_EXPANDED + 5)]
    repository = _GraphRepository({a: neighbours})
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [_candidate(a)], 1000)

    assert len(expanded) <= MAX_EXPANDED


def test_configured_limits_cannot_exceed_hard_caps() -> None:
    expander = RelationExpander(
        _GraphRepository(), max_seeds=10_000, max_expanded=10_000
    )

    assert expander.max_seeds == MAX_SEEDS
    assert expander.max_expanded == MAX_EXPANDED


def test_negative_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        RelationExpander(_GraphRepository(), max_seeds=-1)
    with pytest.raises(ValueError):
        RelationExpander(_GraphRepository(), max_expanded=-1)


def test_empty_seeds_return_no_expansion() -> None:
    repository = _GraphRepository()
    expander = RelationExpander(repository)

    assert expander.expand(_USER, [], 10) == []
    assert repository.related_calls == []


def test_conflict_peers_win_the_budget_over_plain_neighbours() -> None:
    """普通邻居占满预算时，命中冲突组的同伴仍必须出现。"""
    seed_a, peer, neighbour_one, neighbour_two = uuid4(), uuid4(), uuid4(), uuid4()
    group = uuid4()
    repository = _GraphRepository({seed_a: [neighbour_one, neighbour_two]})
    repository.conflict_members = {seed_a: [peer], peer: [seed_a]}
    seed = MemoryCandidate(
        memory_id=seed_a, user_id=_USER, content="a", score=1.0, conflict_group_id=group
    )
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [seed], 2)

    ids = [candidate.memory_id for candidate in expanded]
    assert peer in ids, "冲突组同伴被普通邻居挤掉"
    assert len(ids) <= 2


def test_multiple_conflict_groups_use_deterministic_order() -> None:
    """多个冲突组同时出现时结果与集合迭代顺序无关。"""
    first_seed, first_peer = uuid4(), uuid4()
    second_seed, second_peer = uuid4(), uuid4()
    group_one, group_two = uuid4(), uuid4()
    repository = _GraphRepository()
    repository.conflict_members = {
        first_seed: [first_peer],
        first_peer: [first_seed],
        second_seed: [second_peer],
        second_peer: [second_seed],
    }
    seeds = [
        MemoryCandidate(
            memory_id=first_seed, user_id=_USER, content="a", score=1.0,
            conflict_group_id=group_one,
        ),
        MemoryCandidate(
            memory_id=second_seed, user_id=_USER, content="b", score=0.5,
            conflict_group_id=group_two,
        ),
    ]
    expander = RelationExpander(repository)

    first = expander.expand(_USER, seeds, 2)
    second = expander.expand(_USER, seeds, 2)

    assert [candidate.memory_id for candidate in first] == [
        candidate.memory_id for candidate in second
    ]
    assert len(first) <= 2


def test_conflict_peers_are_completed_and_marked() -> None:
    """冲突组命中一侧时补全另一侧，并保留冲突身份。"""
    a, b = uuid4(), uuid4()
    group = uuid4()
    repository = _GraphRepository()
    repository.conflict_members = {a: [b], b: [a]}
    seed = MemoryCandidate(
        memory_id=a, user_id=_USER, content="a", score=0.0, conflict_group_id=group
    )
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [seed], 10)

    peers = [candidate for candidate in expanded if candidate.memory_id == b]
    assert len(peers) == 1
    assert peers[0].conflict_group_id == group
