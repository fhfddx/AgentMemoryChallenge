"""基线混合检索：全文、文本向量、图片向量与元数据召回 + 加权 RRF。"""

import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from masm.providers.embeddings import EmbeddingProvider
from masm.retrieval.rrf import RankedChannel, reciprocal_rank_fusion
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate

LEXICAL_CHANNEL = "lexical"
TEXT_VECTOR_CHANNEL = "text_vector"
IMAGE_VECTOR_CHANNEL = "image_vector"
METADATA_CHANNEL = "metadata"

# 通道权重是配置项（可用 MASM_RETRIEVAL_WEIGHTS 覆盖），不写死在检索算法里。
DEFAULT_CHANNEL_WEIGHTS: Mapping[str, float] = {
    LEXICAL_CHANNEL: 1.0,
    TEXT_VECTOR_CHANNEL: 1.0,
    IMAGE_VECTOR_CHANNEL: 0.8,
    METADATA_CHANNEL: 0.4,
}

_WEIGHTS_ENV = "MASM_RETRIEVAL_WEIGHTS"

# 独立召回通道的有界并发度；每个通道各自持有数据库 Session。
DEFAULT_CHANNEL_WORKERS = 4


def load_channel_weights(environ: Mapping[str, str] | None = None) -> dict[str, float]:
    """读取通道权重配置（``name=weight,...``）；未设置时使用默认配置。"""
    source = os.environ if environ is None else environ
    raw = source.get(_WEIGHTS_ENV, "").strip()
    if not raw:
        return dict(DEFAULT_CHANNEL_WEIGHTS)
    weights: dict[str, float] = {}
    for item in raw.split(","):
        name, _, value = item.partition("=")
        name = name.strip()
        if not name:
            continue
        weights[name] = float(value)
    return weights


@dataclass(frozen=True)
class ParsedQuery:
    """查询分析结果（对应设计规范 8.1）。"""

    text_queries: Sequence[str] = ()
    visual_queries: Sequence[bytes] = ()
    entities: Sequence[str] = ()
    time_constraints: Sequence[str] = ()
    location_constraints: Sequence[str] = ()
    relation_hints: Sequence[str] = ()
    intent: str = "fact"


class BaselineRetriever:
    """基线混合检索器：所有通道都在同一用户范围内召回。"""

    def __init__(
        self,
        repository: MemoryRepository,
        embeddings: EmbeddingProvider,
        weights: Mapping[str, float],
        *,
        channel_limit: int | None = None,
        max_channel_workers: int = DEFAULT_CHANNEL_WORKERS,
    ) -> None:
        if max_channel_workers < 1:
            raise ValueError("max_channel_workers 必须为正整数")
        self._repo = repository
        self._embeddings = embeddings
        self._weights = dict(weights)
        self._channel_limit = channel_limit
        self._max_channel_workers = max_channel_workers

    def retrieve(self, user_id: str, query: ParsedQuery, limit: int) -> list[MemoryCandidate]:
        """按查询召回并融合，返回不超过 ``limit`` 条候选。"""
        if limit < 1:
            return []
        channels, catalogue = self._recall(user_id, query, limit)
        results: list[MemoryCandidate] = []
        for item in reciprocal_rank_fusion(channels, self._weights):
            if len(results) >= limit:
                break
            source = catalogue.get(item.memory_id)
            if source is None:
                continue
            results.append(
                MemoryCandidate(
                    memory_id=source.memory_id,
                    user_id=source.user_id,
                    content=source.content,
                    score=item.score,
                    supersedes=source.supersedes,
                    status=source.status,
                    conflict_group_id=source.conflict_group_id,
                )
            )
        return results

    def _recall(
        self, user_id: str, query: ParsedQuery, limit: int
    ) -> tuple[list[RankedChannel], dict[UUID, MemoryCandidate]]:
        recall_limit = self._channel_limit or limit
        tasks: list[tuple[str, Callable[[], Sequence[MemoryCandidate]]]] = []

        # 查询向量先生成完毕，随后各通道并行执行；每个通道各自持有数据库 Session。
        text = " ".join(part for part in query.text_queries if part).strip()
        if text:
            tasks.append(
                (
                    LEXICAL_CHANNEL,
                    partial(self._repo.lexical_candidates, user_id, text, recall_limit),
                )
            )
            text_vector = list(self._embeddings.embed_texts([text])[0])
            tasks.append(
                (
                    TEXT_VECTOR_CHANNEL,
                    partial(
                        self._repo.vector_candidates,
                        user_id,
                        text_vector,
                        modality="text",
                        model_name=self._embeddings.model_name,
                        model_version=self._embeddings.model_version,
                        limit=recall_limit,
                    ),
                )
            )

        visual_queries = list(query.visual_queries)
        if visual_queries:
            for image_vector in self._embeddings.embed_images(visual_queries):
                tasks.append(
                    (
                        IMAGE_VECTOR_CHANNEL,
                        partial(
                            self._repo.vector_candidates,
                            user_id,
                            list(image_vector),
                            modality="image",
                            model_name=self._embeddings.model_name,
                            model_version=self._embeddings.model_version,
                            limit=recall_limit,
                        ),
                    )
                )

        modality = "image" if query.intent == "visual" else None
        keywords = tuple(part for part in query.entities if part)
        if modality is not None or keywords:
            tasks.append(
                (
                    METADATA_CHANNEL,
                    partial(
                        self._repo.metadata_candidates,
                        user_id,
                        modality=modality,
                        keywords=keywords,
                        limit=recall_limit,
                    ),
                )
            )

        channels = self._run_channels(tasks)
        catalogue: dict[UUID, MemoryCandidate] = {}
        for channel in channels:
            for candidate in channel.candidates:
                catalogue.setdefault(candidate.memory_id, candidate)
        return channels, catalogue

    def _run_channels(
        self, tasks: Sequence[tuple[str, Callable[[], Sequence[MemoryCandidate]]]]
    ) -> list[RankedChannel]:
        """有界并行执行独立召回通道。

        结果按任务提交顺序收集，因此通道顺序与融合结果始终确定，与完成先后无关。
        """
        if not tasks:
            return []
        if len(tasks) == 1:
            name, call = tasks[0]
            return [RankedChannel(name, call())]
        workers = min(self._max_channel_workers, len(tasks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [(name, pool.submit(call)) for name, call in tasks]
            return [RankedChannel(name, future.result()) for name, future in futures]
