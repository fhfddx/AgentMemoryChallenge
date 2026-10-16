"""Deterministic three-state assembly over a validated evidence graph.

The model never supplies a global verdict. Coverage comes from validated direct/negative
supports. Connectivity comes from validated proposal links plus deterministic stored-conflict
adjacency derived from validated supports. The returned evidence is the smallest connected set
that keeps every covered requirement interpretable. Unrelated candidates are never added to fill
the response budget, and a claimed complete path is never truncated: when it does not fit the
evidence cap the verdict degrades to partial.
"""

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from masm.retrieval.evidence_graph_rules import (
    CandidateView,
    ValidatedLink,
    ValidatedSupport,
    discriminative_units,
)

GraphState = Literal["sufficient", "partial", "insufficient"]

_COVERING_ROLES = frozenset({"direct", "negative"})


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
    question_units: frozenset[str] = frozenset(),
    requirement_units: Sequence[frozenset[str]] = (),
    tighten_completeness: bool = False,
) -> AssemblyOutcome:
    """从已校验的支持与链接推导三态并选出证据。

    ``question_units`` 是问题的区分单元，``requirement_units`` 与需求一一对应。两者用于
    完整性判定：模型没有覆盖、且没有任何已选证据覆盖的问题内容，绝不允许判为完整。

    ``tighten_completeness`` 只在问题含子句边界标记时为真：此时需求引文自身的区分单元也必须
    被已选证据覆盖，否则该需求不算被覆盖。单子句问题（例如 "What color bicycle did I buy?"）
    的省略回答（"No, I bought the red one."）不在此列，必须继续被判为完整。
    """
    if requirement_count < 1:
        return AssemblyOutcome("insufficient", (), 0)
    # 图节点 = 被引用的支撑节点 ∪ 已校验链接的端点；未连接、未被引用的候选永远不进入图。
    cited = {support.candidate_index for support in supports}
    linked = {index for link in links for index in (link.left_index, link.right_index)}
    nodes = sorted(cited | linked)
    adjacency = _adjacency(
        nodes,
        links,
        internal_pairs=_stored_conflict_pairs(supports, candidates),
    )
    interpretive_pairs = tuple(
        (link.left_index, link.right_index)
        for link in links
        if link.kind in {"confirmation", "correction"}
    )
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

    unclaimed = set(question_units)
    for units in requirement_units:
        unclaimed -= units

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
        included = _complete_with_linked_context(
            _deduplicate(
                _inclusion(
                    set(components[component_index]),
                    chosen,
                    support_nodes,
                    adjacency,
                    interpretive_pairs,
                ),
                candidates,
            ),
            component_nodes=set(components[component_index]),
            adjacency=adjacency,
            candidates=candidates,
            question_units=question_units,
            max_selected=max_selected,
        )
        if len(included) > max_selected:
            continue
        evidence_units = _grounded_units(included, candidates)
        if unclaimed - set(evidence_units):
            continue
        if not _requirements_are_grounded(
            chosen, requirement_units, evidence_units, tighten=tighten_completeness
        ):
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
    included = _complete_with_linked_context(
        _deduplicate(
            _inclusion(
                component_nodes,
                chosen,
                support_nodes,
                adjacency,
                interpretive_pairs,
            ),
            candidates,
        ),
        component_nodes=component_nodes,
        adjacency=adjacency,
        candidates=candidates,
        question_units=question_units,
        max_selected=max_selected,
    )
    if len(included) > max_selected:
        included = _best_bounded_partial(
            component_nodes=component_nodes,
            covered=covered,
            covering=covering,
            adjacency=adjacency,
            candidates=candidates,
            supports=supports,
            interpretive_pairs=interpretive_pairs,
            max_selected=max_selected,
        )
    evidence_units = _grounded_units(included, candidates)
    if unclaimed - set(evidence_units):
        return AssemblyOutcome("insufficient", (), len(components))
    remaining = [
        requirement
        for requirement in chosen
        if _requirement_is_grounded(
            requirement, requirement_units, evidence_units, tighten=tighten_completeness
        )
    ]
    if not remaining:
        return AssemblyOutcome("insufficient", (), len(components))
    return AssemblyOutcome("partial", _selected(included, candidates), len(components))


def _requirement_is_grounded(
    requirement: int,
    requirement_units: Sequence[frozenset[str]],
    evidence_units: frozenset[str],
    *,
    tighten: bool,
) -> bool:
    if not tighten or requirement >= len(requirement_units):
        return True
    return requirement_units[requirement] <= evidence_units


def _requirements_are_grounded(
    chosen: Mapping[int, int],
    requirement_units: Sequence[frozenset[str]],
    evidence_units: frozenset[str],
    *,
    tighten: bool,
) -> bool:
    return all(
        _requirement_is_grounded(
            requirement, requirement_units, evidence_units, tighten=tighten
        )
        for requirement in chosen
    )


def _grounded_units(nodes: set[int], candidates: Sequence[CandidateView]) -> frozenset[str]:
    units: set[str] = set()
    for node in nodes:
        units |= discriminative_units(candidates[node].text)
    return frozenset(units)


def _complete_with_linked_context(
    included: set[int],
    *,
    component_nodes: set[int],
    adjacency: Mapping[int, Sequence[int]],
    candidates: Sequence[CandidateView],
    question_units: frozenset[str],
    max_selected: int,
) -> set[int]:
    """补入已证明连接、且能覆盖问题中尚未落地区分单元的邻居（确定性、有上限）。

    候选必须与已选节点处于同一连通分量、由已校验或代码证明的边相连，并且真的能覆盖问题里
    还没有任何引文覆盖的区分单元；因此不会引入无关候选，也不会为凑数而扩张。
    """
    if not question_units:
        return included
    missing = set(question_units) - set(_grounded_units(included, candidates))
    while missing and len(included) < max_selected:
        addition: int | None = None
        for node in sorted(included):
            for neighbour in adjacency[node]:
                if neighbour in included or neighbour not in component_nodes:
                    continue
                if discriminative_units(candidates[neighbour].text) & missing:
                    addition = neighbour
                    break
            if addition is not None:
                break
        if addition is None:
            break
        included.add(addition)
        missing -= discriminative_units(candidates[addition].text)
    return included


def _adjacency(
    nodes: Sequence[int],
    links: Sequence[ValidatedLink],
    *,
    internal_pairs: Sequence[tuple[int, int]] = (),
) -> dict[int, list[int]]:
    adjacency: dict[int, list[int]] = {node: [] for node in nodes}
    pairs = [
        (link.left_index, link.right_index)
        for link in links
    ]
    pairs.extend(internal_pairs)
    for left, right in pairs:
        if left not in adjacency or right not in adjacency:
            continue
        adjacency[left].append(right)
        adjacency[right].append(left)
    for neighbours in adjacency.values():
        neighbours[:] = sorted(set(neighbours))
    return adjacency


def _stored_conflict_pairs(
    supports: Sequence[ValidatedSupport],
    candidates: Sequence[CandidateView],
) -> tuple[tuple[int, int], ...]:
    """Derive internal adjacency only from grounded opposite-role stored peers."""
    requirements = sorted({support.requirement_index for support in supports})
    pairs: list[tuple[int, int]] = []
    for requirement in requirements:
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
        positive_groups = {
            candidates[node].conflict_group_id for node in positives
        } - {None}
        negative_groups = {
            candidates[node].conflict_group_id for node in negatives
        } - {None}
        for group in sorted(positive_groups & negative_groups, key=str):
            members = sorted(
                node
                for node in positives | negatives
                if candidates[node].conflict_group_id == group
            )
            pairs.extend(zip(members, members[1:], strict=False))
    return tuple(pairs)


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
    # Validator caps (4 requirements, 24 supports) bound the largest balanced product
    # to 6^4 = 1296, so every valid interpretation can be examined deterministically.
    return [tuple(combination) for combination in itertools.product(*nodes_per_requirement)]


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
    interpretive_pairs: Sequence[tuple[int, int]],
) -> set[int]:
    included: set[int] = set(chosen.values())
    for requirement, node in chosen.items():
        for support_node in sorted(support_nodes.get(requirement, set())):
            if support_node in component_nodes:
                included |= _shortest_path(adjacency, component_nodes, node, support_node)
    ordered = sorted(chosen.values())
    for left, right in zip(ordered, ordered[1:], strict=False):
        included |= _shortest_path(adjacency, component_nodes, left, right)
    return _interpretive_closure(included, component_nodes, interpretive_pairs)


def _interpretive_closure(
    included: set[int],
    component_nodes: set[int],
    interpretive_pairs: Sequence[tuple[int, int]],
) -> set[int]:
    """Keep the grounded context needed to interpret confirmations and corrections."""
    expanded = set(included)
    changed = True
    while changed:
        changed = False
        for left, right in interpretive_pairs:
            if left not in component_nodes or right not in component_nodes:
                continue
            if left not in expanded and right not in expanded:
                continue
            before = len(expanded)
            expanded.update((left, right))
            changed = changed or len(expanded) != before
    return expanded


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


def _connected_choice(
    component_nodes: set[int],
    chosen_nodes: Iterable[int],
    adjacency: Mapping[int, Sequence[int]],
) -> set[int]:
    """Return the deterministic path union connecting the chosen covering nodes."""
    ordered = sorted(set(chosen_nodes))
    if not ordered:
        return set()
    included = {ordered[0]}
    for left, right in zip(ordered, ordered[1:], strict=False):
        included |= _shortest_path(adjacency, component_nodes, left, right)
    return included


def _is_connected(
    nodes: set[int], adjacency: Mapping[int, Sequence[int]]
) -> bool:
    if not nodes:
        return False
    reached = {min(nodes)}
    stack = list(reached)
    while stack:
        current = stack.pop()
        for neighbour in adjacency[current]:
            if neighbour in nodes and neighbour not in reached:
                reached.add(neighbour)
                stack.append(neighbour)
    return reached == nodes


def _best_bounded_partial(
    *,
    component_nodes: set[int],
    covered: Sequence[int],
    covering: Mapping[int, Sequence[int]],
    adjacency: Mapping[int, Sequence[int]],
    candidates: Sequence[CandidateView],
    supports: Sequence[ValidatedSupport],
    interpretive_pairs: Sequence[tuple[int, int]],
    max_selected: int,
) -> set[int]:
    """Choose the best connected requirement subset that fits the response cap."""
    available = {
        requirement: [node for node in covering[requirement] if node in component_nodes]
        for requirement in covered
    }
    requirements = [requirement for requirement in covered if available[requirement]]
    direct_nodes = {
        support.candidate_index for support in supports if support.role == "direct"
    }
    best: tuple[tuple[int, int, int, int, tuple[int, ...]], set[int]] | None = None
    for subset_size in range(1, len(requirements) + 1):
        for subset in itertools.combinations(requirements, subset_size):
            choices = [available[requirement] for requirement in subset]
            for combination in itertools.product(*choices):
                chosen = set(combination)
                raw = _interpretive_closure(
                    _connected_choice(component_nodes, chosen, adjacency),
                    component_nodes,
                    interpretive_pairs,
                )
                deduplicated = _deduplicate(raw, candidates)
                included = (
                    deduplicated
                    if chosen <= deduplicated and _is_connected(deduplicated, adjacency)
                    else raw
                )
                if len(included) > max_selected:
                    continue
                sources = len({candidates[node].source_group for node in included})
                key = (
                    -len(subset),
                    -len(included & direct_nodes),
                    -sources,
                    len(included),
                    tuple(sorted(included)),
                )
                if best is None or key < best[0]:
                    best = (key, included)
    return best[1] if best is not None else set()


def _selected(included: set[int], candidates: Sequence[CandidateView]) -> tuple[CandidateView, ...]:
    return tuple(candidates[node] for node in sorted(included))
