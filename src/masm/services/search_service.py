"""Search 应用服务（查询分析 -> 混合召回 -> 一跳扩展 -> 冲突感知重排 -> 证据打包）。

未注入 analyzer/expander/reranker 时保持任务 4 的基线行为；官方 Search 请求与响应
Schema 始终不变，且绝不生成最终答案。
"""

from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from masm.retrieval.baseline import BaselineRetriever, ParsedQuery
from masm.retrieval.diagnostics import SearchDiagnostics, emit_search_diagnostics
from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.query_analyzer import QueryAnalyzer, with_option_variants
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.relevance import RelevanceGate
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
        relevance_gate: RelevanceGate | None = None,
        renderer: EvidenceRenderer | None = None,
        packer: ResponsePacker | None = None,
        runtime_profile: str = "unknown",
    ) -> None:
        self._retriever = retriever
        self._max_image_bytes = max_image_bytes
        self._analyzer = analyzer
        self._expander = expander
        self._reranker = reranker
        self._relevance_gate = relevance_gate
        self._renderer = renderer
        self._packer = packer
        self._runtime_profile = runtime_profile

    def search(self, request: SearchRequest) -> SearchResponse:
        """执行查询分析、混合召回、关系扩展与证据重排。"""
        started = perf_counter()
        parsed = self._analyze(request)
        top_k = request.top_k
        # 父子记忆会共同占据召回名额；所有档位都需要先扩大原始候选池。
        pool_limit = min(256, max(top_k, top_k * 3))
        retrieve_with_stats = getattr(self._retriever, "retrieve_with_stats", None)
        if callable(retrieve_with_stats):
            recalled, channel_counts = retrieve_with_stats(request.user_id, parsed, pool_limit)
        else:
            recalled = self._retriever.retrieve(request.user_id, parsed, pool_limit)
            channel_counts = {}
        candidates = [item for item in recalled if item.user_id == request.user_id]
        if self._relevance_gate is not None:
            candidates = self._relevance_gate.filter(candidates)
        if self._expander is not None and candidates:
            candidates = self._with_expansion(request.user_id, candidates, top_k)
        candidate_count = len(candidates)
        if self._expander is not None or self._reranker is not None:
            candidates = self._deduplicate(candidates)
        dedup_count = len(candidates)
        ranked = self._rank(parsed, candidates)
        if self._renderer is not None:
            ranked = self._renderer.render(request.user_id, ranked)
        # 打包保持保留证据的相对顺序，但跳过超大条目以免遮蔽后续短证据。
        packed = (
            self._packer.pack(ranked, top_k) if self._packer is not None else ranked[:top_k]
        )
        response = SearchResponse(
            data=[
                MemoryEvidence(
                    id=str(evidence.memory_id),
                    content=evidence.content,
                    score=evidence.score,
                )
                for evidence in packed
            ]
        )
        emit_search_diagnostics(
            SearchDiagnostics(
                request_tag=uuid4().hex,
                runtime_profile=self._runtime_profile,
                candidate_count=candidate_count,
                dedup_count=dedup_count,
                returned_count=len(response.data),
                response_bytes=len(response.model_dump_json().encode("utf-8")),
                latency_ms=(perf_counter() - started) * 1000.0,
                status_code=200,
                channel_counts=channel_counts,
            )
        )
        return response

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
            parsed = self._analyzer.parse(request.query)
        else:
            parsed = self.analyze(request)
        return with_option_variants(parsed, request.query, request.options)

    def _with_expansion(
        self, user_id: str, candidates: Sequence[MemoryCandidate], top_k: int
    ) -> list[MemoryCandidate]:
        assert self._expander is not None
        repository = getattr(self._retriever, "repository", None)
        seeds = list(candidates)[: self._expander.max_seeds]
        if repository is not None:
            request_ids = [
                item.request_id for item in seeds
                if item.granularity == "message" and item.request_id
            ]
            parents = {
                item.request_id: item
                for item in repository.context_candidates_for_requests(user_id, request_ids)
            }
            # 治理关系只挂在 context 节点：消息命中时用同源父节点作一跳种子。
            seeds = [parents.get(item.request_id, item) for item in seeds]
            seeds = list({item.memory_id: item for item in seeds}.values())
        expanded = self._expander.expand(user_id, seeds, top_k)
        merged: list[MemoryCandidate] = list(candidates)
        known = {candidate.memory_id for candidate in merged}
        for candidate in expanded:
            if candidate.user_id != user_id or candidate.memory_id in known:
                continue
            known.add(candidate.memory_id)
            merged.append(candidate)
        if repository is not None:
            related_runs = [
                item.request_id for item in expanded
                if item.user_id == user_id and item.granularity == "context" and item.request_id
            ]
            for candidate in repository.message_candidates_for_requests(
                user_id, related_runs, min(64, max(8, top_k))
            ):
                if candidate.user_id != user_id or candidate.memory_id in known:
                    continue
                known.add(candidate.memory_id)
                merged.append(candidate)
        return merged

    @staticmethod
    def _deduplicate(candidates: Sequence[MemoryCandidate]) -> list[MemoryCandidate]:
        """按来源位置去重；同源内容完全相同的父记忆让位于消息证据。"""
        messages_by_run: dict[tuple[str, str], set[str]] = {}
        for item in candidates:
            if item.granularity == "message" and item.request_id:
                messages_by_run.setdefault((item.user_id, item.request_id), set()).add(
                    " ".join(item.content.casefold().split())
                )
        result: list[MemoryCandidate] = []
        seen_ids = set()
        seen_sources: set[tuple[str, str, str, int | None]] = set()
        for item in candidates:
            if item.memory_id in seen_ids:
                continue
            seen_ids.add(item.memory_id)
            if item.request_id:
                source = (item.user_id, item.request_id, item.granularity, item.source_position)
                if source in seen_sources:
                    continue
                seen_sources.add(source)
            if (
                item.granularity == "context"
                and item.conflict_group_id is None
                and item.request_id
                and " ".join(item.content.casefold().split())
                in messages_by_run.get((item.user_id, item.request_id), set())
            ):
                continue
            result.append(item)
        return result

    def _rank(
        self, parsed: ParsedQuery, candidates: Sequence[MemoryCandidate]
    ) -> list[RankedEvidence]:
        if self._reranker is not None:
            return self._reranker.rank(parsed, candidates)
        # 基线模式仍不调用生成式模型；同分消息证据优先于同源长上下文。
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.score,
                0 if item.granularity == "message" else 1,
                str(item.memory_id),
            ),
        )
        return [
            RankedEvidence(
                memory_id=candidate.memory_id,
                user_id=candidate.user_id,
                content=candidate.content,
                score=candidate.score,
                rank=position,
                conflict_group_id=candidate.conflict_group_id,
                duplicate_of=candidate.duplicate_of,
                granularity=candidate.granularity,
                request_id=candidate.request_id,
                source_position=candidate.source_position,
            )
            for position, candidate in enumerate(ordered, start=1)
        ]
