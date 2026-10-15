"""Deterministic three-state assembly over a validated evidence graph.

The model never supplies a global verdict. Coverage comes from validated direct/negative
supports, connectivity comes from validated links only, and the returned evidence is the
smallest connected set that keeps every covered requirement interpretable. Unrelated
candidates are never added to fill the response budget, and a claimed complete path is never
truncated: when it does not fit the evidence cap the verdict degrades to partial.
"""

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from masm.retrieval.evidence_graph_rules import (
    CandidateView,
    ValidatedLink,
    ValidatedSupport,
)

GraphState = Literal["sufficient", "partial", "insufficient"]

_COVERING_ROLES = frozenset({"direct", "negative"})
# 覆盖节点组合的确定性枚举上限；超出后退化为每个需求取最小 rank 的覆盖节点。
MAX_ASSEMBLY_COMBINATIONS = 512


@dataclass(frozen=True)
class AssemblyOutcome:
    """组装结果：三态判定 + 原 rank 顺序的入选证据 + 已引用节点连通分量数。"""

    state: GraphState
    selected: tuple[CandidateView, ...]
    component_count: int


def assemble(
    *,
    requirement_count: int,
    supports: Sequence[ValidatedSupport],
    links: Sequence[ValidatedLink],
    candidates: Sequence[CandidateView],
    max_selected: int = 12,
) -> AssemblyOutcome:
    """从已校验的支持与链接推导三态并选出证据。"""
    if requirement_count < 1:
        return AssemblyOutcome("insufficient", (), 0)
    # 图节点 = 被引用的支撑节点 ∪ 已校验链接的端点；未连接、未被引用的候选永远不进入图。
    cited = {support.candidate_index for support in supports}
    linked = {index for link in links for index in (link.left_index, link.right_index)}
    nodes = sorted(cited | linked)
    adjacency = _adjacency(nodes, links)
    components = _components(adjacency)
    component_of = {
        node: index for index, component in enumerate(components) for node in component
    }
    if not cited:
        return AssemblyOutcome("insufficient", (), len(components))

    covering: dict[int, list[int]] = {
        requirement: sorted(
            {
                support.candidate_index
                for support in supports
                if support.requirement_index == requirement
                and support.role in _COVERING_ROLES
            }
        )
        for requirement in range(requirement_count)
    }
    support_nodes: dict[int, set[int]] = {
        requirement: {
            support.candidate_index
            for support in supports
            if support.requirement_index == requirement
        }
        for requirement in range(requirement_count)
    }
    covered = [requirement for requirement in range(requirement_count) if covering[requirement]]
    if not covered:
        return AssemblyOutcome("insufficient", (), len(components))

    resolved = _conflicts_are_resolved(covered, supports, candidates)

    choice_lists = [(requirement, covering[requirement]) for requirement in covered]
    combinations = _combinations(choice_lists)

    best_full: tuple[tuple[int, int, int, tuple[int, ...]], set[int]] | None = None
    for combination in combinations:
        chosen = {
            requirement: node
            for (requirement, _), node in zip(choice_lists, combination, strict=True)
        }
        component_index = _single_component(chosen.values(), component_of)
        if component_index is None:
            continue
        included = _deduplicate(
            _inclusion(
                set(components[component_index]), chosen, support_nodes, adjacency
            ),
            candidates,
        )
        if len(included) > max_selected:
            continue
        score = _score(included, candidates, supports)
        if best_full is None or score < best_full[0]:
            best_full = (score, included)

    if best_full is not None and len(covered) == requirement_count and resolved:
        return AssemblyOutcome(
            "sufficient", _selected(best_full[1], candidates), len(components)
        )

    component_index = _best_component(
        components, covered, covering, support_nodes, candidates, supports
    )
    component_nodes = set(components[component_index])
    chosen = {
        requirement: min(node for node in covering[requirement] if node in component_nodes)
        for requirement in covered
        if any(node in component_nodes for node in covering[requirement])
    }
    included = _deduplicate(
        _inclusion(component_nodes, chosen, support_nodes, adjacency), candidates
    )
    if len(included) > max_selected:
        included = set(_bounded(included, chosen.values(), max_selected))
    return AssemblyOutcome("partial", _selected(included, candidates), len(components))


def _adjacency(nodes: Sequence[int], links: Sequence[ValidatedLink]) -> dict[int, list[int]]:
    adjacency: dict[int, list[int]] = {node: [] for node in nodes}
    for link in links:
        if link.left_index not in adjacency or link.right_index not in adjacency:
            continue
        adjacency[link.left_index].append(link.right_index)
        adjacency[link.right_index].append(link.left_index)
    for neighbours in adjacency.values():
        neighbours.sort()
    return adjacency


def _components(adjacency: Mapping[int, Sequence[int]]) -> list[tuple[int, ...]]:
    visited: set[int] = set()
    components: list[tuple[int, ...]] = []
    for node in sorted(adjacency):
        if node in visited:
            continue
        visited.add(node)
        stack = [node]
        members: list[int] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbour in adjacency[current]:
                if neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(neighbour)
        components.append(tuple(sorted(members)))
    components.sort(key=lambda component: component[0])
    return components


def _conflicts_are_resolved(
    covered: Sequence[int],
    supports: Sequence[ValidatedSupport],
    candidates: Sequence[CandidateView],
) -> bool:
    """正负支持必须同属一个已存储冲突组；否则三态降级为 partial。"""
    for requirement in covered:
        positives = {
            support.candidate_index
            for support in supports
            if support.requirement_index == requirement and support.role == "direct"
        }
        negatives = {
            support.candidate_index
            for support in supports
            if support.requirement_index == requirement and support.role == "negative"
        }
        if not positives or not negatives:
            continue
        groups = {
            candidates[node].conflict_group_id for node in positives | negatives
        }
        if len(groups) != 1 or None in groups:
            return False
    return True


def _combinations(
    choice_lists: Sequence[tuple[int, Sequence[int]]],
) -> list[tuple[int, ...]]:
    nodes_per_requirement = [list(nodes) for _, nodes in choice_lists]
    total = 1
    for nodes in nodes_per_requirement:
        total *= max(1, len(nodes))
    if total <= MAX_ASSEMBLY_COMBINATIONS:
        return [tuple(combination) for combination in itertools.product(*nodes_per_requirement)]
    return [tuple(nodes[0] for nodes in nodes_per_requirement)]


def _single_component(chosen: Iterable[int], component_of: Mapping[int, int]) -> int | None:
    indices = {component_of[node] for node in chosen}
    if len(indices) != 1:
        return None
    return indices.pop()


def _shortest_path(
    adjacency: Mapping[int, Sequence[int]], allowed: set[int], start: int, goal: int
) -> set[int]:
    if start == goal:
        return {start}
    previous: dict[int, int] = {}
    queue = [start]
    seen = {start}
    while queue:
        current = queue.pop(0)
        for neighbour in adjacency[current]:
            if neighbour not in allowed or neighbour in seen:
                continue
            seen.add(neighbour)
            previous[neighbour] = current
            if neighbour == goal:
                path = {goal}
                cursor = goal
                while cursor != start:
                    cursor = previous[cursor]
                    path.add(cursor)
                return path
            queue.append(neighbour)
    return {start}


def _inclusion(
    component_nodes: set[int],
    chosen: Mapping[int, int],
    support_nodes: Mapping[int, set[int]],
    adjacency: Mapping[int, Sequence[int]],
) -> set[int]:
    included: set[int] = set(chosen.values())
    for requirement, node in chosen.items():
        for support_node in sorted(support_nodes.get(requirement, set())):
            if support_node in component_nodes:
                included |= _shortest_path(adjacency, component_nodes, node, support_node)
    ordered = sorted(chosen.values())
    for left, right in zip(ordered, ordered[1:], strict=False):
        included |= _shortest_path(adjacency, component_nodes, left, right)
    return included


def _deduplicate(included: set[int], candidates: Sequence[CandidateView]) -> set[int]:
    """同一来源位置的重复表示只保留最靠前的一个。"""
    kept: dict[tuple[str, int | None], int] = {}
    duplicates: set[int] = set()
    for node in sorted(included):
        view = candidates[node]
        if not view.request_id or view.source_position is None:
            continue
        key = (view.request_id, view.source_position)
        if key in kept:
            duplicates.add(node)
        else:
            kept[key] = node
    return included - duplicates


def _score(
    included: set[int], candidates: Sequence[CandidateView], supports: Sequence[ValidatedSupport]
) -> tuple[int, int, int, tuple[int, ...]]:
    """先少节点，再多 direct 支持，再多来源，最后按原 rank 稳定排序。"""
    direct_nodes = {
        support.candidate_index for support in supports if support.role == "direct"
    }
    direct = len(included & direct_nodes)
    sources = len({candidates[node].source_group for node in included})
    return (len(included), -direct, -sources, tuple(sorted(included)))


def _best_component(
    components: Sequence[tuple[int, ...]],
    covered: Sequence[int],
    covering: Mapping[int, Sequence[int]],
    support_nodes: Mapping[int, set[int]],
    candidates: Sequence[CandidateView],
    supports: Sequence[ValidatedSupport],
) -> int:
    best_index = 0
    best_key: tuple[int, int, int, int, int] | None = None
    for index, component in enumerate(components):
        members = set(component)
        covered_here = sum(
            1 for requirement in covered if any(node in members for node in covering[requirement])
        )
        if not covered_here:
            continue
        direct_nodes = {
            support.candidate_index for support in supports if support.role == "direct"
        }
        key = (
            -covered_here,
            -len(members & direct_nodes),
            -len({candidates[node].source_group for node in members}),
            len(members),
            component[0],
        )
        if best_key is None or key < best_key:
            best_key = key
            best_index = index
    return best_index


def _bounded(
    included: set[int], covering_nodes: Iterable[int], max_selected: int
) -> list[int]:
    """超出上限时先保留每个需求的覆盖节点，再按原 rank 补入连接节点。"""
    essential = sorted(set(covering_nodes))
    selected = list(essential[:max_selected])
    selected_set = set(selected)
    for node in sorted(included):
        if len(selected) >= max_selected:
            break
        if node not in selected_set:
            selected.append(node)
            selected_set.add(node)
    return sorted(selected)


def _selected(included: set[int], candidates: Sequence[CandidateView]) -> tuple[CandidateView, ...]:
    return tuple(candidates[node] for node in sorted(included))
