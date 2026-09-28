"""Search 应用服务（查询分析 -> 混合召回 -> 一跳扩展 -> 冲突感知重排 -> 证据打包）。

未注入 analyzer/expander/reranker 时保持任务 4 的基线行为；官方 Search 请求与响应
Schema 始终不变，且绝不生成最终答案。
"""

from collections.abc import Sequence

from masm.retrieval.baseline import BaselineRetriever, ParsedQuery
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.reranker import EvidenceReranker, RankedEvidence
from masm.retrieval.response_packer import ResponsePacker
from masm.schemas.api import MemoryEvidence, SearchRequest, SearchResponse
from masm.storage.assets import decode_image_data_url
from masm.storage.types import MemoryCandidate


class SearchService:
    """Search 服务：不做最终答案生成，只返回命中证据。"""

    def __init__(
        self,
        retriever: BaselineRetriever,
        *,
        max_image_bytes: int,
        analyzer: QueryAnalyzer | None = None,
        expander: RelationExpander | None = None,
        reranker: EvidenceReranker | None = None,
        packer: ResponsePacker | None = None,
    ) -> None:
        self._retriever = retriever
        self._max_image_bytes = max_image_bytes
        self._analyzer = analyzer
        self._expander = expander
        self._reranker = reranker
        self._packer = packer

    def search(self, request: SearchRequest) -> SearchResponse:
        """执行查询分析、混合召回、关系扩展与证据重排。"""
        parsed = self._analyze(request)
        top_k = request.top_k
        candidates = self._retriever.retrieve(request.user_id, parsed, top_k)
        if self._expander is not None:
            candidates = self._with_expansion(request.user_id, candidates, top_k)
        ranked = self._rank(parsed, candidates)
        # 重排顺序不因打包改变；打包只做严格前缀裁剪。
        packed = (
            self._packer.pack(ranked, top_k) if self._packer is not None else ranked[:top_k]
        )
        return SearchResponse(
            data=[
                MemoryEvidence(
                    id=str(evidence.memory_id),
                    content=evidence.content,
                    score=evidence.score,
                )
                for evidence in packed
            ]
        )

    def analyze(self, request: SearchRequest) -> ParsedQuery:
        """规则化查询分析：纯文本查询或保持顺序的多模态查询。"""
        query = request.query
        if isinstance(query, str):
            return ParsedQuery(text_queries=(query,), intent="fact")

        texts: list[str] = []
        images: list[bytes] = []
        for part in query:
            if part.type == "text":
                texts.append(part.text)
            else:
                image = decode_image_data_url(part.image_url.url, self._max_image_bytes)
                images.append(image.data)
        return ParsedQuery(
            text_queries=tuple(texts),
            visual_queries=tuple(images),
            intent="visual" if images else "fact",
        )

    def _analyze(self, request: SearchRequest) -> ParsedQuery:
        if self._analyzer is not None:
            return self._analyzer.parse(request.query)
        return self.analyze(request)

    def _with_expansion(
        self, user_id: str, candidates: Sequence[MemoryCandidate], top_k: int
    ) -> list[MemoryCandidate]:
        assert self._expander is not None
        expanded = self._expander.expand(user_id, list(candidates), top_k)
        merged: list[MemoryCandidate] = list(candidates)
        known = {candidate.memory_id for candidate in merged}
        for candidate in expanded:
            if candidate.memory_id in known:
                continue
            known.add(candidate.memory_id)
            merged.append(candidate)
        return merged

    def _rank(
        self, parsed: ParsedQuery, candidates: Sequence[MemoryCandidate]
    ) -> list[RankedEvidence]:
        if self._reranker is not None:
            return self._reranker.rank(parsed, candidates)
        # 基线模式：只按召回分数排序，不做任何生成式处理。
        ordered = sorted(candidates, key=lambda item: (-item.score, str(item.memory_id)))
        return [
            RankedEvidence(
                memory_id=candidate.memory_id,
                user_id=candidate.user_id,
                content=candidate.content,
                score=candidate.score,
                rank=position,
                conflict_group_id=candidate.conflict_group_id,
                duplicate_of=candidate.duplicate_of,
            )
            for position, candidate in enumerate(ordered, start=1)
        ]
