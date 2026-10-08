"""Runtime Factory 的档位装配测试。"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from masm.api.app import create_app
from masm.config import RuntimeProfile, Settings
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider, FakeStructuredLLM
from masm.providers.llm import OpenAICompatibleLLM
from masm.providers.multimodal_embeddings import GroundedMultimodalEmbeddingProvider
from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.evidence_selector import DeterministicEvidenceSelector, EvidenceSelector
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.relevance import RelevanceGate
from masm.retrieval.reranker import EvidenceReranker
from masm.runtime import build_runtime
from masm.storage.assets import AssetStore
from masm.storage.db import Database
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
    assert runtime.relevance_gate is None
    assert isinstance(runtime.evidence_selector, DeterministicEvidenceSelector)
    assert runtime.retriever._text_queries_search_images is False  # noqa: SLF001


def test_application_factory_wires_original_evidence_renderer() -> None:
    app = create_app(
        _settings(RuntimeProfile.LOCAL_FAKE),
        database=Mock(spec=Database),
        asset_store=Mock(spec=AssetStore),
    )

    assert isinstance(app.state.search_service._renderer, EvidenceRenderer)  # noqa: SLF001


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
    assert runtime.relevance_gate is None
    assert runtime.evidence_selector is None
    assert runtime.retriever._text_queries_search_images is True  # noqa: SLF001


def test_app_shutdown_closes_runtime_owned_llm_client() -> None:
    """应用生命周期结束时不遗留模型 HTTP 连接池。"""
    app = create_app(
        _settings(RuntimeProfile.OFFICIAL_MASM),
        database=Mock(spec=Database),
        embeddings=OfficialTestEmbeddings(),
    )
    llm = app.state.runtime.llm
    assert isinstance(llm, OpenAICompatibleLLM)
    client = llm._http_client()  # noqa: SLF001
    assert client.is_closed is False

    with TestClient(app):
        pass

    assert client.is_closed is True


def test_app_shutdown_closes_runtime_owned_embedding_client() -> None:
    app = create_app(_settings(RuntimeProfile.OFFICIAL_MASM), database=Mock(spec=Database))
    embeddings = app.state.runtime.embeddings
    assert isinstance(embeddings, GroundedMultimodalEmbeddingProvider)
    client = embeddings._text_embeddings._http_client()  # noqa: SLF001
    assert client.is_closed is False

    with TestClient(app):
        pass

    assert client.is_closed is True


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
    assert isinstance(runtime.relevance_gate, RelevanceGate)
    assert isinstance(runtime.evidence_selector, EvidenceSelector)
    assert runtime.relevance_gate.min_text_similarity == 0.48
    assert runtime.relevance_gate.min_image_similarity == 0.42
    assert runtime.relevance_gate.min_lexical_rank == 0.001
    assert runtime.retriever._text_queries_search_images is True  # noqa: SLF001


def test_official_masm_can_disable_selection_without_disabling_recall() -> None:
    from dataclasses import replace

    runtime = build_runtime(
        replace(_settings(RuntimeProfile.OFFICIAL_MASM), selector_enabled=False),
        _repository(), embeddings=OfficialTestEmbeddings(), llm=FakeStructuredLLM(),
    )

    assert runtime.evidence_selector is None
    assert isinstance(runtime.relevance_gate, RelevanceGate)


def test_official_masm_wires_fused_text_only_when_enabled() -> None:
    from dataclasses import replace

    disabled = build_runtime(
        _settings(RuntimeProfile.OFFICIAL_MASM), _repository(),
        embeddings=OfficialTestEmbeddings(), llm=FakeStructuredLLM(),
    )
    enabled = build_runtime(
        replace(_settings(RuntimeProfile.OFFICIAL_MASM), fused_empty_history_text=True),
        _repository(), embeddings=OfficialTestEmbeddings(), llm=FakeStructuredLLM(),
    )

    assert disabled.add_pipeline is not None
    assert enabled.add_pipeline is not None
    assert disabled.add_pipeline._fused_text is None
    assert enabled.add_pipeline._fused_text is not None
    assert disabled.audit_metadata()["fused_empty_history_text"] is False
    assert enabled.audit_metadata()["fused_empty_history_text"] is True
    assert enabled.audit_metadata()["prompt_versions"]["fused_text"] == "fused-text-v2"
