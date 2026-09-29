"""Runtime Factory 的档位装配测试。"""

from unittest.mock import Mock

import pytest

from masm.config import RuntimeProfile, Settings
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider, FakeStructuredLLM
from masm.providers.multimodal_embeddings import GroundedMultimodalEmbeddingProvider
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.reranker import EvidenceReranker
from masm.runtime import build_runtime
from masm.storage.repositories import MemoryRepository


class OfficialTestEmbeddings(DeterministicFakeEmbeddingProvider):
    """只用于依赖注入；名称模拟正式空间，但不访问外部服务。"""

    model_name = "text-embedding-v4"
    model_version = "cycle2-fixed"


def _settings(profile: RuntimeProfile) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://postgres@localhost/masm",
        runtime_profile=profile,
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-secret",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="embedding-secret",
    )


def _repository() -> MemoryRepository:
    return Mock(spec=MemoryRepository)


def test_local_fake_builds_no_llm_or_advanced_pipeline() -> None:
    runtime = build_runtime(_settings(RuntimeProfile.LOCAL_FAKE), _repository())

    assert isinstance(runtime.embeddings, DeterministicFakeEmbeddingProvider)
    assert runtime.llm is None
    assert runtime.add_pipeline is None
    assert runtime.query_analyzer is None
    assert runtime.relation_expander is None
    assert runtime.reranker is None


def test_local_fake_ignores_unrelated_model_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MASM_LLM_API_KEY", "must-not-be-used")
    monkeypatch.setenv("MASM_EMBEDDING_API_KEY", "must-not-be-used")
    monkeypatch.delenv("MASM_RUNTIME_PROFILE", raising=False)
    settings = Settings.from_env()

    runtime = build_runtime(settings, _repository())

    assert runtime.profile is RuntimeProfile.LOCAL_FAKE
    assert runtime.llm is None
    assert isinstance(runtime.embeddings, DeterministicFakeEmbeddingProvider)


def test_official_baseline_builds_real_embeddings_without_add_pipeline() -> None:
    runtime = build_runtime(_settings(RuntimeProfile.OFFICIAL_BASELINE), _repository())

    assert isinstance(runtime.embeddings, GroundedMultimodalEmbeddingProvider)
    assert runtime.embeddings.model_name == "text-embedding-v4"
    assert runtime.llm is not None
    assert runtime.llm.model == "gpt-4o-mini"
    assert runtime.add_pipeline is None
    assert runtime.query_analyzer is None
    assert runtime.relation_expander is None
    assert runtime.reranker is None


def test_official_masm_builds_three_agents_and_all_search_components() -> None:
    llm = FakeStructuredLLM(model="gpt-4o-mini")
    runtime = build_runtime(
        _settings(RuntimeProfile.OFFICIAL_MASM),
        _repository(),
        embeddings=OfficialTestEmbeddings(),
        llm=llm,
    )

    assert isinstance(runtime.embeddings, EmbeddingProvider)
    assert runtime.llm is llm
    assert isinstance(runtime.add_pipeline, AddPipeline)
    assert runtime.add_pipeline._perception._llm is llm
    assert runtime.add_pipeline._temporal._llm is llm
    assert runtime.add_pipeline._curator._llm is llm
    assert isinstance(runtime.query_analyzer, QueryAnalyzer)
    assert isinstance(runtime.relation_expander, RelationExpander)
    assert isinstance(runtime.reranker, EvidenceReranker)
