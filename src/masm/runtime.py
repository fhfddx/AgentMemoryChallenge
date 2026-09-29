"""按运行档位装配模型、AddPipeline 与 Search 组件。"""

from collections.abc import Mapping
from dataclasses import dataclass

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.config import RuntimeProfile, Settings
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.providers.multimodal_embeddings import GroundedMultimodalEmbeddingProvider
from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider
from masm.providers.reranker import LexicalReranker
from masm.retrieval.baseline import BaselineRetriever, load_channel_weights
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.reranker import EvidenceReranker
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

    def audit_metadata(self) -> dict[str, object]:
        """返回不含密钥和内容的运行时审计元数据。"""
        prompt_versions: dict[str, str] = {}
        if self.add_pipeline is not None:
            prompt_versions = {
                "perception": self.add_pipeline._perception.prompt_version,
                "temporal": self.add_pipeline._temporal.prompt_version,
                "curator": self.add_pipeline._curator.prompt_version,
            }
        return {
            "profile": self.profile.value,
            "embedding_model": self.embeddings.model_name,
            "embedding_version": self.embeddings.model_version,
            "llm_model": self.llm.model if self.llm is not None else None,
            "prompt_versions": prompt_versions,
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
    )
    add_pipeline: AddPipeline | None = None
    query_analyzer: QueryAnalyzer | None = None
    relation_expander: RelationExpander | None = None
    reranker: EvidenceReranker | None = None

    if profile is RuntimeProfile.OFFICIAL_MASM:
        assert selected_llm is not None
        add_pipeline = AddPipeline(
            perception=PerceptionAgent(selected_llm),
            temporal=TemporalRelationAgent(selected_llm),
            curator=MemoryCuratorAgent(selected_llm),
            retriever=retriever,
        )
        query_analyzer = QueryAnalyzer(
            llm=selected_llm,
            max_image_bytes=settings.max_image_bytes,
        )
        relation_expander = RelationExpander(repository)
        reranker = EvidenceReranker(provider=LexicalReranker())

    return RuntimeComponents(
        profile=profile,
        embeddings=selected_embeddings,
        llm=selected_llm,
        retriever=retriever,
        add_pipeline=add_pipeline,
        query_analyzer=query_analyzer,
        relation_expander=relation_expander,
        reranker=reranker,
    )
