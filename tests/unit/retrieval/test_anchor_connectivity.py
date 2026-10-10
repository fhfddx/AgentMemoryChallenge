"""有界锚点关系可达性：只沿已持久化的同用户关系边扩展。"""

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from masm.retrieval.anchor_connectivity import (
    DEFAULT_HOP_LIMIT,
    MAX_ANCHOR_HOPS,
    MAX_HOP_LIMIT,
    AnchorConnectivity,
)
from masm.storage.types import MemoryCandidate


def _candidate(number: int, run: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=UUID(int=number),
        user_id="user-1",
        content=f"memory {number}",
        score=0.0,
        request_id=run,
    )


class _FakeRelationGraph:
    """按 memory ID 连边的假图；同时记录每次读取的参数以验证有界与同用户。"""

    def __init__(
        self,
        *,
        edges: dict[int, list[int]] | None = None,
        contexts: dict[str, int] | None = None,
    ) -> None:
        self._edges = edges or {}
        self._contexts = contexts or {}
        self.related_calls: list[tuple[str, list[UUID], int]] = []
        self.context_calls: list[tuple[str, list[str]]] = []

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[MemoryCandidate]:
        self.context_calls.append((user_id, list(request_ids)))
        return [
            _candidate(self._contexts[run], run)
            for run in request_ids
            if run in self._contexts
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        self.related_calls.append((user_id, list(memory_ids), limit))
        neighbours = [
            _candidate(target, f"run-{target}")
            for memory_id in memory_ids
            for target in self._edges.get(memory_id.int, ())
        ]
        return neighbours[:limit]


def test_anchor_without_any_edge_only_connects_itself_and_its_run() -> None:
    """隔离记录的邻居为 0：只能联通自身记忆与其所属运行。"""
    anchor_id = UUID(int=4)
    graph = _FakeRelationGraph(contexts={"run-isolated": 104})

    connected = AnchorConnectivity(graph).connected(
        "user-1", [anchor_id], ["run-isolated"], max_hops=2
    )

    assert connected.memory_ids == frozenset({anchor_id, UUID(int=104)})
    assert connected.request_ids == frozenset({"run-isolated"})
    assert graph.related_calls == [
        ("user-1", [anchor_id, UUID(int=104)], DEFAULT_HOP_LIMIT)
    ]


def test_message_anchor_is_mapped_to_its_context_parent_before_traversal() -> None:
    """治理关系挂在 context 上：message 锚点必须先用同源 context 作一跳种子。"""
    message_id = UUID(int=1)
    graph = _FakeRelationGraph(
        edges={101: [102], 102: [103]},
        contexts={"run-root": 101},
    )

    connected = AnchorConnectivity(graph).connected(
        "user-1", [message_id], ["run-root"], max_hops=2
    )

    assert graph.context_calls == [("user-1", ["run-root"])]
    assert graph.related_calls[0][1] == [message_id, UUID(int=101)]
    assert connected.request_ids == frozenset({"run-root", "run-102", "run-103"})


def test_second_hop_requires_two_hops_and_is_capped_by_max_hops() -> None:
    """一跳预算只能取到桥节点，两跳预算才取到下游端点。"""
    graph = _FakeRelationGraph(
        edges={101: [102], 102: [103]},
        contexts={"run-root": 101},
    )
    connectivity = AnchorConnectivity(graph)

    one_hop = connectivity.connected("user-1", [], ["run-root"], max_hops=1)
    assert one_hop.request_ids == frozenset({"run-root", "run-102"})

    graph.related_calls.clear()
    two_hops = connectivity.connected("user-1", [], ["run-root"], max_hops=2)
    assert two_hops.request_ids == frozenset({"run-root", "run-102", "run-103"})


def test_zero_hops_reads_no_relations() -> None:
    """max_hops=0 时只返回锚点自身与同源 context，不做任何关系读取。"""
    graph = _FakeRelationGraph(edges={101: [102]}, contexts={"run-root": 101})

    connected = AnchorConnectivity(graph).connected(
        "user-1", [], ["run-root"], max_hops=0
    )

    assert connected.request_ids == frozenset({"run-root"})
    assert graph.related_calls == []


def test_cycles_terminate_and_emit_each_memory_once() -> None:
    """环、往复边与 diamond 都不得让遍历重复扩展或死循环。"""
    graph = _FakeRelationGraph(
        edges={101: [102, 103], 102: [101, 104], 103: [104], 104: [101]},
        contexts={"run-root": 101},
    )

    connected = AnchorConnectivity(graph).connected(
        "user-1", [], ["run-root"], max_hops=MAX_ANCHOR_HOPS
    )

    assert connected.memory_ids == frozenset(
        {UUID(int=101), UUID(int=102), UUID(int=103), UUID(int=104)}
    )
    assert 1 <= len(graph.related_calls) <= MAX_ANCHOR_HOPS


def test_every_read_is_scoped_to_the_requested_user() -> None:
    """两次读取都必须带同一 user_id：隔离在 SQL 阶段完成。"""
    graph = _FakeRelationGraph(edges={101: [102]}, contexts={"run-root": 101})

    AnchorConnectivity(graph).connected("user-a", [], ["run-root"], max_hops=1)

    assert {call[0] for call in graph.context_calls} == {"user-a"}
    assert {call[0] for call in graph.related_calls} == {"user-a"}


def test_hop_limit_bounds_every_relation_read() -> None:
    """单跳读取量受 hop_limit 限制，不会因为关系图拥挤而放大。"""
    graph = _FakeRelationGraph(
        edges={101: [201, 202, 203]},
        contexts={"run-root": 101},
    )

    connected = AnchorConnectivity(graph, hop_limit=1).connected(
        "user-1", [], ["run-root"], max_hops=1
    )

    assert graph.related_calls[0][2] == 1
    assert connected.request_ids == frozenset({"run-root", "run-201"})


def test_missing_request_ids_skip_the_context_lookup() -> None:
    """没有 request_id 时不发无意义的 context 查询。"""
    graph = _FakeRelationGraph(edges={101: [102]})
    memory_id = uuid4()

    connected = AnchorConnectivity(graph).connected(
        "user-1", [memory_id], [], max_hops=2
    )

    assert graph.context_calls == []
    assert connected.memory_ids == frozenset({memory_id})


@pytest.mark.parametrize("hop_limit", [0, -1, MAX_HOP_LIMIT + 1])
def test_invalid_hop_limit_is_rejected(hop_limit: int) -> None:
    with pytest.raises(ValueError, match="hop_limit"):
        AnchorConnectivity(_FakeRelationGraph(), hop_limit=hop_limit)


@pytest.mark.parametrize("max_hops", [-1, MAX_ANCHOR_HOPS + 1])
def test_invalid_max_hops_is_rejected(max_hops: int) -> None:
    with pytest.raises(ValueError, match="max_hops"):
        AnchorConnectivity(_FakeRelationGraph()).connected(
            "user-1", [], [], max_hops=max_hops
        )
