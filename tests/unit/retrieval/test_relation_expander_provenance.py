"""Provenance sidecar is additive: expand() is unchanged and the sidecar is bounded."""

from uuid import UUID

from masm.retrieval.relation_expander import (
    MAX_PROVENANCE_EDGES,
    ExpansionEdge,
    RelationExpander,
)
from masm.storage.types import MemoryCandidate, RelationEdge

_USER = "user-1"


def _candidate(memory_id: UUID, content: str = "memory") -> MemoryCandidate:
    return MemoryCandidate(memory_id=memory_id, user_id=_USER, content=content, score=0.0)


class _ProvenanceRepository:
    """关系邻居与关系边分开记录，用于证明 expand() 不读取溯源。"""

    def __init__(self) -> None:
        self.adjacency: dict[UUID, list[UUID]] = {}
        self.rows: list[RelationEdge] = []
        self.related_calls: list[list[UUID]] = []
        self.provenance_calls: list[list[UUID]] = []

    def related(self, user_id: str, memory_ids: list[UUID], limit: int) -> list[MemoryCandidate]:
        assert user_id == _USER
        seeds = list(dict.fromkeys(memory_ids))
        self.related_calls.append(seeds)
        known = set(seeds)
        found: list[MemoryCandidate] = []
        for seed in seeds:
            for neighbour in self.adjacency.get(seed, []):
                if neighbour in known:
                    continue
                known.add(neighbour)
                found.append(_candidate(neighbour))
        return found[:limit]

    def conflict_peers(
        self, user_id: str, memory_ids: list[UUID], limit: int
    ) -> list[MemoryCandidate]:
        assert user_id == _USER
        del memory_ids, limit
        return []

    def relation_edges(
        self, user_id: str, memory_ids: list[UUID], limit: int
    ) -> list[RelationEdge]:
        assert user_id == _USER
        self.provenance_calls.append(list(memory_ids))
        allowed = set(memory_ids)
        return [
            row
            for row in self.rows
            if row.source_id in allowed and row.target_id in allowed
        ][:limit]


def _edge(left: UUID, right: UUID, relation_type: str = "follows") -> RelationEdge:
    return RelationEdge(source_id=left, target_id=right, relation_type=relation_type)


def test_expand_never_reads_provenance_and_keeps_its_shape() -> None:
    a, b, c = UUID(int=1), UUID(int=2), UUID(int=3)
    repository = _ProvenanceRepository()
    repository.adjacency = {a: [b], b: [c]}
    repository.rows = [_edge(a, b), _edge(b, c)]
    expander = RelationExpander(repository)

    expanded = expander.expand(_USER, [_candidate(a)], 10)

    assert [item.memory_id for item in expanded] == [b, c]
    assert isinstance(expanded, list)
    assert repository.related_calls == [[a], [b]]
    assert repository.provenance_calls == []


def test_expand_with_provenance_returns_identical_candidates_plus_a_sidecar() -> None:
    a, b, c = UUID(int=1), UUID(int=2), UUID(int=3)
    repository = _ProvenanceRepository()
    repository.adjacency = {a: [b], b: [c]}
    repository.rows = [_edge(a, b, "supersedes"), _edge(b, c, "same_topic")]
    expander = RelationExpander(repository)

    plain = RelationExpander(repository).expand(_USER, [_candidate(a)], 10)
    result = expander.expand_with_provenance(_USER, [_candidate(a)], 10)

    assert list(result.candidates) == plain
    assert [item.memory_id for item in result.candidates] == [b, c]
    # 种子前沿、种子加一跳前沿、以及第二跳种子各查询一次有界溯源。
    assert repository.provenance_calls == [[a], [a, b], [b, c]]
    assert [(edge.predecessor_id, edge.candidate_id) for edge in result.edges] == [
        (a, b),
        (b, c),
    ]
    assert [edge.relation_type for edge in result.edges] == ["supersedes", "same_topic"]
    assert [edge.depth for edge in result.edges] == [1, 2]


def test_provenance_drops_edges_whose_other_endpoint_was_not_traversed() -> None:
    a, b, stranger = UUID(int=1), UUID(int=2), UUID(int=99)
    repository = _ProvenanceRepository()
    repository.adjacency = {a: [b]}
    repository.rows = [_edge(a, b), _edge(a, stranger)]
    result = RelationExpander(repository).expand_with_provenance(_USER, [_candidate(a)], 10)

    assert [(edge.predecessor_id, edge.candidate_id) for edge in result.edges] == [(a, b)]


def test_provenance_records_each_row_once_and_is_bounded() -> None:
    a = UUID(int=1)
    neighbours = [UUID(int=index + 2) for index in range(MAX_PROVENANCE_EDGES + 5)]
    repository = _ProvenanceRepository()
    repository.adjacency = {a: neighbours}
    repository.rows = [_edge(a, neighbour) for neighbour in neighbours] * 2
    result = RelationExpander(repository).expand_with_provenance(_USER, [_candidate(a)], 100)

    keys = [(edge.predecessor_id, edge.candidate_id, edge.relation_type) for edge in result.edges]
    assert len(keys) == len(set(keys))
    assert len(result.edges) <= MAX_PROVENANCE_EDGES


def test_provenance_is_empty_for_seeds_without_relations() -> None:
    a = UUID(int=1)
    repository = _ProvenanceRepository()
    repository.adjacency = {}
    result = RelationExpander(repository).expand_with_provenance(_USER, [_candidate(a)], 10)

    assert result.candidates == ()
    assert result.edges == ()
    # 种子前沿始终会被查询：两端都已被直接召回的真实关系边也必须可证。
    assert repository.provenance_calls == [[a]]


def test_conflict_completion_is_not_reported_as_relation_provenance() -> None:
    a, peer = UUID(int=1), UUID(int=2)
    group = UUID(int=777)

    class _ConflictRepository(_ProvenanceRepository):
        def conflict_peers(
            self, user_id: str, memory_ids: list[UUID], limit: int
        ) -> list[MemoryCandidate]:
            assert user_id == _USER
            del memory_ids, limit
            return [
                MemoryCandidate(
                    memory_id=peer,
                    user_id=_USER,
                    content="conflicting",
                    score=0.0,
                    conflict_group_id=group,
                )
            ]

    repository = _ConflictRepository()
    repository.rows = [_edge(a, peer)]
    seed = MemoryCandidate(
        memory_id=a,
        user_id=_USER,
        content="conflicting side",
        score=0.0,
        conflict_group_id=group,
    )
    result = RelationExpander(repository).expand_with_provenance(_USER, [seed], 10)

    assert [item.memory_id for item in result.candidates] == [peer]
    assert result.edges == ()


def test_expansion_edge_is_a_frozen_record() -> None:
    edge = ExpansionEdge(
        predecessor_id=UUID(int=1),
        candidate_id=UUID(int=2),
        relation_type="follows",
        depth=1,
    )
    assert edge.depth == 1
    assert hash(edge) == hash(edge)
