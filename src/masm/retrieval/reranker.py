"""冲突感知的证据重排。

重排器只产生相关性、排序与证据元数据；它从不生成答案正文，也不改写证据内容。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from masm.providers.reranker import RerankerProvider, resolve_rerank_input
from masm.retrieval.baseline import ParsedQuery
from masm.schemas.content import ContentPart
from masm.storage.types import MemoryCandidate

# 最终证据上限与 Provider 输入上限分离，保持公开 top_k=100 契约。
MAX_RERANK_CANDIDATES = 100

_ENTITY_BONUS = 0.10
_LOCATION_BONUS = 0.08
_TIME_BONUS = 0.05
_RELATION_BONUS = 0.05
_CONFLICT_BONUS = 0.05
_DUPLICATE_PENALTY = 0.20
_PROVIDER_WEIGHT = 0.30


@dataclass(frozen=True)
class RankedEvidence:
    """一条经过重排的证据。

    只包含相关性、排序与证据来源元数据；没有任何答案或生成文本字段。
    """

    memory_id: UUID
    user_id: str
    content: str | list[ContentPart]
    score: float
    rank: int
    matched_entities: Sequence[str] = ()
    matched_locations: Sequence[str] = ()
    matched_relations: Sequence[str] = ()
    matched_time: bool = False
    conflict_group_id: UUID | None = None
    is_conflict_peer: bool = False
    duplicate_of: UUID | None = None
    provider_score: float | None = None
    metadata: Mapping[str, float] = field(default_factory=dict)
    granularity: Literal["context", "message"] = "context"
    request_id: str = ""
    source_position: int | None = None


class EvidenceReranker:
    """确定性规则重排 + 可选轻量相关性 Provider。"""

    def __init__(
        self,
        *,
        provider: RerankerProvider | None = None,
        max_candidates: int | None = None,
    ) -> None:
        self._provider = provider
        if max_candidates is not None and max_candidates < 0:
            raise ValueError("max_candidates 必须为非负整数")
        self._max_candidates = min(
            MAX_RERANK_CANDIDATES if max_candidates is None else max_candidates,
            MAX_RERANK_CANDIDATES,
        )

    @property
    def max_candidates(self) -> int:
        """参与重排的候选硬上限。"""
        return self._max_candidates

    def rank(
        self, query: ParsedQuery, candidates: Sequence[MemoryCandidate]
    ) -> list[RankedEvidence]:
        """按相关性、匹配度、冲突覆盖与重复惩罚排序。"""
        if not candidates:
            return []
        pool = self._admit(candidates)
        provider_scores = self._provider_scores(query, pool)
        group_counts: dict[UUID, int] = {}
        for candidate in pool:
            if candidate.conflict_group_id is not None:
                group_counts[candidate.conflict_group_id] = (
                    group_counts.get(candidate.conflict_group_id, 0) + 1
                )

        scored: list[RankedEvidence] = []
        for index, candidate in enumerate(pool):
            scored.append(
                self._score(query, candidate, provider_scores[index], group_counts)
            )
        granularity = {candidate.memory_id: candidate.granularity for candidate in pool}
        scored.sort(
            key=lambda item: (
                -item.score,
                0 if granularity[item.memory_id] == "message" else 1,
                str(item.memory_id),
            )
        )

        ranked: list[RankedEvidence] = []
        seen_groups: set[UUID] = set()
        for position, evidence in enumerate(scored, start=1):
            is_peer = False
            group_id = evidence.conflict_group_id
            if group_id is not None:
                is_peer = group_id in seen_groups
                seen_groups.add(group_id)
            ranked.append(
                RankedEvidence(
                    memory_id=evidence.memory_id,
                    user_id=evidence.user_id,
                    content=evidence.content,
                    score=evidence.score,
                    rank=position,
                    matched_entities=evidence.matched_entities,
                    matched_locations=evidence.matched_locations,
                    matched_relations=evidence.matched_relations,
                    matched_time=evidence.matched_time,
                    conflict_group_id=group_id,
                    is_conflict_peer=is_peer,
                    duplicate_of=evidence.duplicate_of,
                    provider_score=evidence.provider_score,
                    metadata=evidence.metadata,
                    granularity=evidence.granularity,
                    request_id=evidence.request_id,
                    source_position=evidence.source_position,
                )
            )
        return ranked

    def _admit(self, candidates: Sequence[MemoryCandidate]) -> list[MemoryCandidate]:
        """有界候选准入策略。

        先保证需要补全的冲突证据（同组 ≥2 条）进入重排池，剩余名额按确定性的
        基础分数与 memory_id 选取；既不截断调用方输入前缀，也不突破硬上限。
        """
        by_id: dict[UUID, MemoryCandidate] = {}
        for candidate in candidates:
            by_id.setdefault(candidate.memory_id, candidate)

        grouped: dict[UUID, list[MemoryCandidate]] = {}
        for candidate in by_id.values():
            if candidate.conflict_group_id is not None:
                grouped.setdefault(candidate.conflict_group_id, []).append(candidate)

        selected: list[MemoryCandidate] = []
        selected_ids: set[UUID] = set()
        for group_id in sorted(grouped, key=str):
            members = grouped[group_id]
            if len(members) < 2:
                continue
            for member in sorted(members, key=lambda item: (-item.score, str(item.memory_id))):
                if len(selected) >= self._max_candidates:
                    return selected
                if member.memory_id in selected_ids:
                    continue
                selected.append(member)
                selected_ids.add(member.memory_id)
        if len(selected) >= self._max_candidates:
            return selected

        rest = sorted(
            (item for key, item in by_id.items() if key not in selected_ids),
            key=lambda item: (
                -item.score,
                0 if item.granularity == "message" else 1,
                str(item.memory_id),
            ),
        )
        for candidate in rest:
            if len(selected) >= self._max_candidates:
                break
            selected.append(candidate)
        return selected

    def _score(
        self,
        query: ParsedQuery,
        candidate: MemoryCandidate,
        provider_score: float | None,
        group_counts: Mapping[UUID, int],
    ) -> RankedEvidence:
        haystack = candidate.content.lower()
        entities = tuple(
            entity for entity in query.entities if entity and entity.lower() in haystack
        )
        locations = tuple(
            place
            for place in query.location_constraints
            if place and place.lower() in haystack
        )
        relations = tuple(
            hint for hint in query.relation_hints if hint and hint.lower() in haystack
        )
        matched_time = any(
            constraint and constraint.lower() in haystack for constraint in query.time_constraints
        )

        score = candidate.score
        score += _ENTITY_BONUS * min(len(entities), 3)
        score += _LOCATION_BONUS * min(len(locations), 3)
        score += _RELATION_BONUS * min(len(relations), 3)
        if matched_time:
            score += _TIME_BONUS

        group_id = candidate.conflict_group_id
        if group_id is not None and group_counts.get(group_id, 0) > 1:
            # 冲突组覆盖：保留双方并让它们一起进入结果。
            score += _CONFLICT_BONUS

        if candidate.duplicate_of is not None:
            # 重复惩罚：副本排在原始证据之后。
            score -= _DUPLICATE_PENALTY

        if provider_score is not None:
            score += _PROVIDER_WEIGHT * provider_score

        return RankedEvidence(
            memory_id=candidate.memory_id,
            user_id=candidate.user_id,
            content=candidate.content,
            score=round(score, 6),
            rank=0,
            matched_entities=entities,
            matched_locations=locations,
            matched_relations=relations,
            matched_time=matched_time,
            conflict_group_id=group_id,
            duplicate_of=candidate.duplicate_of,
            provider_score=provider_score,
            metadata={"base_score": candidate.score, **candidate.retrieval_signals},
            granularity=candidate.granularity,
            request_id=candidate.request_id,
            source_position=candidate.source_position,
        )

    def _provider_scores(
        self, query: ParsedQuery, pool: Sequence[MemoryCandidate]
    ) -> list[float | None]:
        """调用可选 Provider；失败或返回非数值时全部退回规则排序。"""
        if self._provider is None:
            return [None] * len(pool)
        text = " ".join(query.text_queries).strip()
        provider_pool = pool[:resolve_rerank_input(self._max_candidates)]
        try:
            raw = self._provider.score(text, [candidate.content for candidate in provider_pool])
        except Exception:
            return [None] * len(pool)
        if len(raw) != len(provider_pool):
            return [None] * len(pool)
        scores: list[float | None] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return [None] * len(pool)
            scores.append(float(value))
        return [*scores, *([None] * (len(pool) - len(provider_pool)))]
