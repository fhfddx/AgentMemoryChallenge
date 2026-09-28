"""Add -> Search -> 打包完整路径的跨用户隔离测试（发布阻断项）。"""

import base64
import io
from uuid import uuid4

from PIL import Image
from sqlalchemy import select

from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.reranker import EvidenceReranker
from masm.retrieval.response_packer import ResponsePacker
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory
from masm.storage.repositories import MemoryRepository


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _data_url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


def _seed(service: AddService, user_id: str, text: str) -> None:
    request = AddRequest(
        request_id=_uid("r"),
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": _data_url()}},
                ],
            }
        ],
    )
    assert service.add(request).success is True


def _owners(database: Database, ids: list[str]) -> set[str]:
    from uuid import UUID

    with database.session() as session:
        return set(
            session.execute(
                select(Memory.user_id).where(Memory.id.in_([UUID(value) for value in ids]))
            ).scalars()
        )


def _full_path_service(database: Database, settings, embeddings) -> SearchService:
    repository = MemoryRepository(database)
    return SearchService(
        BaselineRetriever(repository, embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
        analyzer=QueryAnalyzer(max_image_bytes=settings.max_image_bytes),
        expander=RelationExpander(repository),
        reranker=EvidenceReranker(),
        packer=ResponsePacker(max_bytes=settings.max_search_response_bytes),
    )


def test_full_path_never_leaks_other_users_evidence(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """两个用户写入完全相同的内容，完整路径只能返回目标用户的证据。"""
    user_a, user_b = _uid("user-a"), _uid("user-b")
    text = "identical shared memory about the same green square"
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    _seed(add, user_a, text)
    _seed(add, user_b, text)

    response = _full_path_service(database, settings, embeddings).search(
        SearchRequest(query=text, user_id=user_a, top_k=100)
    )

    ids = [evidence.id for evidence in response.data]
    assert ids
    assert _owners(database, ids) == {user_a}


def test_full_path_with_top_k_100_is_bounded(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_a, user_b = _uid("user-a"), _uid("user-b")
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    for index in range(5):
        _seed(add, user_a, f"shared bounded memory {index}")
        _seed(add, user_b, f"shared bounded memory {index}")

    response = _full_path_service(database, settings, embeddings).search(
        SearchRequest(query="shared bounded memory", user_id=user_a, top_k=100)
    )

    ids = [evidence.id for evidence in response.data]
    assert len(ids) <= 100
    assert _owners(database, ids) == {user_a}


def test_baseline_mode_still_works_without_enhancements(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """未启用增强组件时任务 4 基线模式仍可运行。"""
    user_id = _uid("u")
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    _seed(add, user_id, "baseline isolation memory")

    service = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    )
    response = service.search(SearchRequest(query="baseline isolation", user_id=user_id, top_k=10))

    ids = [evidence.id for evidence in response.data]
    assert ids
    assert _owners(database, ids) == {user_id}
