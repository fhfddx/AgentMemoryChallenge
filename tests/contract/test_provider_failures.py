"""模型 Provider 故障的 HTTP 与持久化边界。"""

import base64
import io
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from masm.api.app import create_app
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.errors import ProviderUnavailableError
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


class FailingEmbeddings(EmbeddingProvider):
    model_name = "text-embedding-v4"
    model_version = "cycle2-fixed"
    dimensions = 64

    def embed_texts(self, texts):
        raise ProviderUnavailableError("private-provider-body provider-secret")

    def embed_images(self, images):
        raise ProviderUnavailableError("private-provider-body provider-secret")


def _client(settings, database, asset_store, embeddings) -> TestClient:
    return TestClient(
        create_app(
            settings,
            database=database,
            asset_store=asset_store,
            embeddings=embeddings,
        ),
        raise_server_exceptions=False,
    )


def _payload(user_id: str, request_id: str, content: object) -> dict:
    return {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": "provider-failure-session",
        "messages": [{"role": "user", "content": content}],
    }


def _data_url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (20, 80, 120)).save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def test_add_provider_failure_returns_503_without_sensitive_detail(
    settings, database: Database, asset_store: AssetStore
) -> None:
    client = _client(settings, database, asset_store, FailingEmbeddings())

    response = client.post(
        "/add",
        headers={"X-Api-Key": "test-key"},
        json=_payload(f"u-{uuid4().hex}", f"r-{uuid4().hex}", "provider failure"),
    )

    assert response.status_code == 503
    rendered = response.text
    assert "模型依赖不可用" in rendered
    assert "private-provider-body" not in rendered
    assert "provider-secret" not in rendered


def test_search_provider_failure_returns_503_without_sensitive_detail(
    settings, database: Database, asset_store: AssetStore
) -> None:
    user_id = f"u-{uuid4().hex}"
    AddService(
        MemoryRepository(database),
        asset_store,
        settings,
        embeddings=DeterministicFakeEmbeddingProvider(),
    ).add(
        AddRequest.model_validate(
            _payload(user_id, f"r-{uuid4().hex}", "search provider failure")
        )
    )
    client = _client(settings, database, asset_store, FailingEmbeddings())

    response = client.post(
        "/search",
        headers={"X-Api-Key": "test-key"},
        json={"query": "provider failure", "user_id": user_id, "top_k": 10},
    )

    assert response.status_code == 503
    assert "模型依赖不可用" in response.text
    assert "private-provider-body" not in response.text
    assert "provider-secret" not in response.text


def test_add_provider_failure_leaves_no_ledger_memory_or_staged_asset(
    settings, database: Database, asset_store: AssetStore
) -> None:
    user_id = f"u-{uuid4().hex}"
    request_id = f"r-{uuid4().hex}"
    client = _client(settings, database, asset_store, FailingEmbeddings())

    response = client.post(
        "/add",
        headers={"X-Api-Key": "test-key"},
        json=_payload(
            user_id,
            request_id,
            [{"type": "image_url", "image_url": {"url": _data_url()}}],
        ),
    )

    repository = MemoryRepository(database)
    assert response.status_code == 503
    assert repository.get_ledger(user_id, request_id) is None
    assert repository.get_by_request(user_id, request_id) is None
    assert not asset_store.base_dir.exists() or not any(asset_store.base_dir.rglob("*"))
