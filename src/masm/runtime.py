"""按运行档位装配模型、AddPipeline 与 Search 组件。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.fused_text import FusedTextAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.config import RuntimeProfile, Settings
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.providers.multimodal_embeddings import GroundedMultimodalEmbeddingProvider
from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider
from masm.providers.reranker import HttpReranker, LexicalReranker
from masm.retrieval.baseline import BaselineRetriever, load_channel_weights
from masm.retrieval.evidence_graph import EvidenceGraphPlanner, GraphEvidenceSelector
from masm.retrieval.evidence_graph_rules import relation_capitalized_names
from masm.retrieval.evidence_selector import DeterministicEvidenceSelector, EvidenceSelector
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.relevance import RelevanceGate
from masm.retrieval.reranker import EvidenceReranker, RankedEvidence
from masm.storage.repositories import MemoryRepository


@dataclass(frozen=True)
class RuntimeComponents:
    """一次应用启动所需的完整、不可变运行时依赖。"""

    profile: RuntimeProfile
    embeddings: EmbeddingProvider
    llm: StructuredLLM | None
    retriever: BaselineRetriever
    add_pipeline: AddPipeline | None
    query_analyzer: QueryAnalyzer | None
    relation_expander: RelationExpander | None
    reranker: EvidenceReranker | None
    relevance_gate: RelevanceGate | None
    evidence_selector: EvidenceSelector | DeterministicEvidenceSelector | None
    evidence_graph: GraphEvidenceSelector | None = None
    refusal_gate: Callable[[str, str], bool] | None = None

    def close(self) -> None:
        """关闭运行时自身创建的模型与 Embedding HTTP 连接池。"""
        if isinstance(self.llm, OpenAICompatibleLLM):
            self.llm.close()
        if isinstance(self.embeddings, GroundedMultimodalEmbeddingProvider):
            self.embeddings.close()

    def audit_metadata(self) -> dict[str, object]:
        """返回不含密钥和内容的运行时审计元数据。"""
        prompt_versions: dict[str, str] = {}
        if self.add_pipeline is not None:
            prompt_versions = {
                "perception": self.add_pipeline._perception.prompt_version,
                "temporal": self.add_pipeline._temporal.prompt_version,
                "curator": self.add_pipeline._curator.prompt_version,
            }
            if self.add_pipeline._fused_text is not None:
                prompt_versions["fused_text"] = self.add_pipeline._fused_text.prompt_version
        return {
            "profile": self.profile.value,
            "embedding_model": self.embeddings.model_name,
            "embedding_version": self.embeddings.model_version,
            "llm_model": self.llm.model if self.llm is not None else None,
            "prompt_versions": prompt_versions,
            "fused_empty_history_text": (
                self.add_pipeline is not None and self.add_pipeline._fused_text is not None
            ),
            "evidence_selector_enabled": isinstance(self.evidence_selector, EvidenceSelector),
        }


def build_runtime(
    settings: Settings,
    repository: MemoryRepository,
    *,
    embeddings: EmbeddingProvider | None = None,
    llm: StructuredLLM | None = None,
    channel_weights: Mapping[str, float] | None = None,
) -> RuntimeComponents:
    """根据显式档位构造运行时；正式档位绝不回退到 Fake。"""
    settings.validate_runtime()
    profile = settings.runtime_profile

    if profile is RuntimeProfile.LOCAL_FAKE:
        selected_llm: StructuredLLM | None = None
        selected_embeddings = embeddings or DeterministicFakeEmbeddingProvider()
    else:
        selected_llm = llm or OpenAICompatibleLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=settings.model_max_attempts,
        )
        if embeddings is not None:
            selected_embeddings = embeddings
        else:
            text_embeddings = OpenAICompatibleEmbeddingProvider(
                model=settings.embedding_model,
                model_version=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key,
                timeout_seconds=settings.model_timeout_seconds,
                max_attempts=settings.model_max_attempts,
            )
            selected_embeddings = GroundedMultimodalEmbeddingProvider(
                text_embeddings=text_embeddings,
                perception=PerceptionAgent(selected_llm),
            )

    retriever = BaselineRetriever(
        repository,
        selected_embeddings,
        channel_weights if channel_weights is not None else load_channel_weights(),
        text_queries_search_images=profile is not RuntimeProfile.LOCAL_FAKE,
    )
    add_pipeline: AddPipeline | None = None
    query_analyzer: QueryAnalyzer | None = None
    relation_expander: RelationExpander | None = None
    reranker: EvidenceReranker | None = None
    relevance_gate: RelevanceGate | None = None
    evidence_selector: EvidenceSelector | DeterministicEvidenceSelector | None = None
    evidence_graph: GraphEvidenceSelector | None = None
    refusal_gate: Callable[[str, str], bool] | None = None

    if profile is RuntimeProfile.LOCAL_FAKE:
        evidence_selector = DeterministicEvidenceSelector()

    if profile is RuntimeProfile.OFFICIAL_MASM:
        assert selected_llm is not None
        add_pipeline = AddPipeline(
            perception=PerceptionAgent(selected_llm),
            temporal=TemporalRelationAgent(selected_llm),
            curator=MemoryCuratorAgent(selected_llm),
            retriever=retriever,
            fused_text=FusedTextAgent(selected_llm) if settings.fused_empty_history_text else None,
        )
        query_analyzer = QueryAnalyzer(
            llm=selected_llm,
            max_image_bytes=settings.max_image_bytes,
        )
        relation_expander = RelationExpander(repository)
        rerank_provider: LexicalReranker | HttpReranker
        if settings.rerank_model:
            # 官方规则不限制 Reranker：三项齐全时用 HTTP 重排，否则退回词面重排。
            rerank_provider = HttpReranker(
                base_url=settings.rerank_base_url,
                api_key=settings.rerank_api_key,
                model=settings.rerank_model,
                timeout_seconds=settings.rerank_timeout_seconds,
            )
        else:
            rerank_provider = LexicalReranker()
        reranker = EvidenceReranker(provider=rerank_provider)
        relevance_gate = RelevanceGate(
            min_lexical_rank=settings.min_lexical_rank,
            min_text_similarity=settings.min_text_similarity,
            min_image_similarity=settings.min_image_similarity,
        )
        if settings.selector_enabled:
            evidence_selector = EvidenceSelector(
                selected_llm,
                max_candidates=settings.selector_max_candidates,
                max_selected=settings.selector_max_selected,
                max_chars_per_candidate=settings.selector_max_chars_per_candidate,
                timeout_seconds=settings.selector_timeout_seconds,
            )
            # 选择器之前的拒答预检：判据与 A 同源，但判在调用选择器之前。
            refusal_gate = _relation_absent_gate(repository)
            if settings.evidence_graph_enabled:
                # 图路径复用同一个 v7 selector 作为唯一回退；默认档位完全不构造它。
                evidence_graph = GraphEvidenceSelector(
                    EvidenceGraphPlanner(
                        selected_llm,
                        max_candidates=settings.selector_max_candidates,
                        max_chars_per_candidate=settings.selector_max_chars_per_candidate,
                        max_requirements=settings.evidence_graph_max_requirements,
                        max_supports=settings.evidence_graph_max_supports,
                        max_links=settings.evidence_graph_max_links,
                        timeout_seconds=settings.evidence_graph_timeout_seconds,
                    ),
                    evidence_selector,
                    max_candidates=settings.selector_max_candidates,
                    max_selected=settings.selector_max_selected,
                    presence=repository.present_tokens,
                    retrieve_more=_supplementary_recall(repository),
                    frames=_frame_texts(repository),
                )

    return RuntimeComponents(
        profile=profile,
        embeddings=selected_embeddings,
        llm=selected_llm,
        retriever=retriever,
        add_pipeline=add_pipeline,
        query_analyzer=query_analyzer,
        relation_expander=relation_expander,
        reranker=reranker,
        relevance_gate=relevance_gate,
        evidence_selector=evidence_selector,
        evidence_graph=evidence_graph,
        refusal_gate=refusal_gate,
    )

def _supplementary_recall(
    repository: MemoryRepository,
) -> "Callable[[str, Sequence[str], int], Sequence[RankedEvidence]]":
    """图路径专用的有界补召回：把 OR 语义的候选包装成重排后的证据。"""

    def retrieve(
        user_id: str, units: Sequence[str], limit: int
    ) -> Sequence[RankedEvidence]:
        candidates = repository.or_lexical_candidates(
            user_id, " ".join(units), limit
        )
        return tuple(
            RankedEvidence(
                memory_id=candidate.memory_id,
                user_id=candidate.user_id,
                content=candidate.content,
                score=candidate.score,
                rank=index,
                granularity=candidate.granularity,
                request_id=candidate.request_id,
                source_position=candidate.source_position,
            )
            for index, candidate in enumerate(candidates)
        )

    return retrieve

def _frame_texts(repository: MemoryRepository) -> "Callable[[str, str], Sequence[str]]":
    """返回"同框架近失配"候选的文本：供 R10 判断是否真的无法回答。"""

    def frame_texts(user_id: str, question: str) -> Sequence[str]:
        candidates = repository.frame_matches(user_id, question, limit=5)
        return tuple(str(candidate.content) for candidate in candidates)

    return frame_texts

def _relation_absent_gate(repository: MemoryRepository) -> Callable[[str, str], bool]:
    """选择器之外的拒答预检：问题点名了全库不存在的关系前缀人。

    判据与选择器内部的 A 相同，但判在调用选择器之前：全库里没有这个关系人时，任何"同属性、
    不同主体"的证据都答非所问，且用户从不用关系前缀指代自己，因此不影响 direct 问题。
    """

    def gate(user_id: str, question: str) -> bool:
        names = relation_capitalized_names(question)
        if not names:
            return False
        present = repository.present_tokens(user_id, sorted(names))
        return bool(names - present)

    return gate
