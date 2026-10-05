"""正式 MASM 档位通过公共 HTTP 接口的装配集成测试。"""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from masm.api.app import create_app
from masm.config import RuntimeProfile, Settings
from masm.providers.fakes import DeterministicFakeEmbeddingProvider, FakeStructuredLLM
from masm.schemas.agents import ActionKind, CuratorAction, CuratorDecision
from masm.storage.assets import AssetStore
from masm.storage.db import Database

_ROOT = Path(__file__).resolve().parents[2]


class OfficialTestEmbeddings(DeterministicFakeEmbeddingProvider):
    model_name = "text-embedding-v4"
    model_version = "cycle2-fixed"


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        api_keys=("test-key",),
        runtime_profile=RuntimeProfile.OFFICIAL_MASM,
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-secret",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="embedding-secret",
    )


def _llm() -> FakeStructuredLLM:
    return FakeStructuredLLM(
        [
            {"keywords": ["runtime", "marker"], "language": "en"},
            {},
            CuratorDecision(
                actions=(
                    CuratorAction(
                        kind=ActionKind.CREATE,
                        confidence=0.9,
                        evidence="runtime marker",
                    ),
                )
            ),
        ],
        model="gpt-4o-mini",
    )


def _client(
    database: Database, asset_store: AssetStore, database_url: str, llm: FakeStructuredLLM
) -> TestClient:
    return TestClient(
        create_app(
            _settings(database_url),
            database=database,
            asset_store=asset_store,
            embeddings=OfficialTestEmbeddings(),
            llm=llm,
        )
    )


def test_official_masm_http_add_skips_curator_without_history(
    database: Database,
    asset_store: AssetStore,
    database_url: str,
) -> None:
    llm = _llm()
    client = _client(database, asset_store, database_url, llm)
    user_id = f"runtime-user-{uuid4().hex}"

    response = client.post(
        "/add",
        headers={"X-Api-Key": "test-key"},
        json={
            "request_id": f"runtime-request-{uuid4().hex}",
            "user_id": user_id,
            "session_id": "runtime-session",
            "messages": [{"role": "user", "content": "runtime marker evidence"}],
        },
    )

    assert response.status_code == 200
    assert [record.output_type for record in llm.records] == [
        "PerceptionResult",
        "TemporalRelationResult",
    ]


def test_official_masm_search_returns_evidence_not_generated_answer(
    database: Database,
    asset_store: AssetStore,
    database_url: str,
) -> None:
    llm = _llm()
    client = _client(database, asset_store, database_url, llm)
    user_id = f"runtime-user-{uuid4().hex}"
    request_id = f"runtime-request-{uuid4().hex}"
    headers = {"X-Api-Key": "test-key"}
    client.post(
        "/add",
        headers=headers,
        json={
            "request_id": request_id,
            "user_id": user_id,
            "session_id": "runtime-session",
            "messages": [{"role": "user", "content": "runtime marker evidence"}],
        },
    )

    response = client.post(
        "/search",
        headers=headers,
        json={"query": "runtime marker", "user_id": user_id, "top_k": 10},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]
    assert body["data"][0]["content"] == "runtime marker evidence"
    assert set(body["data"][0]) <= {"id", "content", "score", "created_at"}
    assert len(llm.records) == 2


def test_app_state_exposes_only_nonsensitive_runtime_metadata(
    database: Database,
    asset_store: AssetStore,
    database_url: str,
) -> None:
    llm = _llm()
    app = create_app(
        _settings(database_url),
        database=database,
        asset_store=asset_store,
        embeddings=OfficialTestEmbeddings(),
        llm=llm,
    )

    metadata = app.state.runtime_metadata
    rendered = json.dumps(metadata, ensure_ascii=False)
    assert metadata == {
        "profile": "official-masm",
        "embedding_model": "text-embedding-v4",
        "embedding_version": "cycle2-fixed",
        "llm_model": "gpt-4o-mini",
        "prompt_versions": {"perception": "v1", "temporal": "v1", "curator": "v1"},
        "fused_empty_history_text": False,
    }
    assert "llm-secret" not in rendered
    assert "embedding-secret" not in rendered


def test_official_profile_requires_competition_embedding_shape(database_url: str) -> None:
    settings = _settings(database_url)
    settings.validate_runtime()
    with pytest.raises(ValueError, match="1024"):
        replace(settings, embedding_dimensions=768).validate_runtime()
    with pytest.raises(ValueError, match="text-embedding-v4"):
        replace(settings, embedding_model="other-model").validate_runtime()


def test_v11_package_keeps_models_and_storage_separate() -> None:
    compose = (_ROOT / "deployments" / "docker-compose.v11.yml").read_text(encoding="utf-8")
    caddy = (_ROOT / "deployments" / "Caddyfile").read_text(encoding="utf-8")
    assert "MASM_RUNTIME_PROFILE: ${MASM_V11_RUNTIME_PROFILE:-official-masm}" in compose
    assert "MASM_LLM_MODEL: gpt-4o-mini" in compose
    assert "MASM_EMBEDDING_MODEL: text-embedding-v4" in compose
    assert "MASM_EMBEDDING_DIMENSIONS: 1024" in compose
    assert "masm-edge" in compose
    assert "masm-v11-api" in compose
    assert "v11-local-test-key" not in compose
    assert "masm-v11-local-only" not in compose
    assert "masm-v11-api:8000" in caddy
    assert "reverse_proxy api:8000" in caddy


def test_docker_context_excludes_local_evaluation_artifacts() -> None:
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".superpowers" in ignored
