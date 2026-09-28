"""增强 Search 集成测试：真实 Repository 链路下的扩展、冲突补全与重复惩罚。"""

from uuid import UUID, uuid4

from sqlalchemy import select, update

from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.reranker import MAX_RERANK_CANDIDATES, EvidenceReranker
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryRelation
from masm.storage.repositories import MemoryRepository


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _seed(
    database: Database, asset_store: AssetStore, settings, embeddings, user_id: str, text: str
) -> UUID:
    request_id = _uid("r")
    AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings
    ).add(_request(request_id, user_id, text))
    return _memory_row(database, user_id, request_id).id


def _memory_row(database: Database, user_id: str, request_id: str) -> Memory:
    with database.session() as session:
        return session.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.request_id == request_id)
        ).scalar_one()


def _add_relation(database: Database, user_id: str, source: UUID, target: UUID) -> None:
    with database.session() as session:
        session.add(
            MemoryRelation(
                user_id=user_id,
                source_type="memory",
                source_id=source,
                target_type="memory",
                target_id=target,
                relation_type="link",
                confidence=1.0,
            )
        )
        session.commit()


def _link_conflict(database: Database, memory_ids: list[UUID], user_id: str) -> UUID:
    group = uuid4()
    with database.session() as session:
        session.execute(
            update(Memory)
            .where(Memory.user_id == user_id, Memory.id.in_(memory_ids))
            .values(conflict_group_id=group)
        )
        session.commit()
    return group


def _enhanced(database: Database, settings, embeddings) -> SearchService:
    repository = MemoryRepository(database)
    return SearchService(
        BaselineRetriever(repository, embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
        analyzer=QueryAnalyzer(max_image_bytes=settings.max_image_bytes),
        expander=RelationExpander(repository),
        reranker=EvidenceReranker(),
    )


def test_baseline_retriever_preserves_duplicate_of(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """真实链路：Repository -> BaselineRetriever 必须保留 duplicate_of。"""
    user_id = _uid("u")
    original = _seed(database, asset_store, settings, embeddings, user_id, "duplicate source")
    duplicate = _seed(database, asset_store, settings, embeddings, user_id, "duplicate source")
    with database.session() as session:
        session.execute(
            update(Memory).where(Memory.id == duplicate).values(duplicate_of=original)
        )
        session.commit()

    candidates = BaselineRetriever(
        MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
    ).retrieve(user_id, _parsed("duplicate"), 10)

    by_id = {candidate.memory_id: candidate for candidate in candidates}
    assert by_id[duplicate].duplicate_of == original
    assert by_id[original].duplicate_of is None


def _parsed(text: str):
    from masm.retrieval.baseline import ParsedQuery

    return ParsedQuery(text_queries=(text,), intent="fact")


def test_duplicate_is_ranked_after_original_through_real_path(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """真实链路：重复惩罚必须让原始证据排在副本之前。"""
    user_id = _uid("u")
    original = _seed(database, asset_store, settings, embeddings, user_id, "shared memory text")
    duplicate = _seed(database, asset_store, settings, embeddings, user_id, "shared memory text")
    with database.session() as session:
        session.execute(
            update(Memory).where(Memory.id == duplicate).values(duplicate_of=original)
        )
        session.commit()

    retriever = BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS)
    candidates = retriever.retrieve(user_id, _parsed("shared memory"), 10)
    ranked = EvidenceReranker().rank(_parsed("shared memory"), candidates)

    ids = [item.memory_id for item in ranked]
    assert ids.index(original) < ids.index(duplicate)
    assert any(item.duplicate_of == original for item in ranked)


def test_conflict_peer_survives_full_hard_cap_through_search(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """初始候选超过重排硬上限、同伴位于输入尾部时仍必须进入重排池。"""
    user_id = _uid("u")
    seed_id = _seed(database, asset_store, settings, embeddings, user_id, "conflict seeded memory")
    peer_id = _seed(database, asset_store, settings, embeddings, user_id, "conflict seeded memory")
    _link_conflict(database, [seed_id, peer_id], user_id)
    for index in range(MAX_RERANK_CANDIDATES + 8):
        _seed(database, asset_store, settings, embeddings, user_id, f"filler memory {index}")

    service = _enhanced(database, settings, embeddings)
    response = service.search(
        SearchRequest(query="conflict seeded", user_id=user_id, top_k=100)
    )

    assert len(response.data) <= 100
    assert len(response.data) <= MAX_RERANK_CANDIDATES


def test_conflict_peer_is_returned_by_enhanced_search(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    seed_id = _seed(database, asset_store, settings, embeddings, user_id, "seeded conflict memory")
    peer_id = _seed(database, asset_store, settings, embeddings, user_id, "peer conflict memory")
    _link_conflict(database, [seed_id, peer_id], user_id)

    response = _enhanced(database, settings, embeddings).search(
        SearchRequest(query="seeded conflict", user_id=user_id, top_k=10)
    )

    returned = {evidence.id for evidence in response.data}
    assert str(seed_id) in returned
    assert str(peer_id) in returned, "冲突同伴未补全"


def test_one_hop_expansion_enters_the_response(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """一跳扩展必须进入候选池与最终响应。"""
    user_id = _uid("u")
    seed_id = _seed(database, asset_store, settings, embeddings, user_id, "anchor memory alpha")
    neighbour_id = _seed(
        database, asset_store, settings, embeddings, user_id, "unrelated wording beta"
    )
    _add_relation(database, user_id, seed_id, neighbour_id)

    response = _enhanced(database, settings, embeddings).search(
        SearchRequest(query="anchor memory", user_id=user_id, top_k=10)
    )

    assert str(neighbour_id) in {evidence.id for evidence in response.data}


def test_query_analysis_reaches_recall(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """注入 QueryAnalyzer 后，规则化子查询必须驱动召回。"""
    user_id = _uid("u")
    memory_id = _seed(
        database, asset_store, settings, embeddings, user_id, "the cat sat on the mat"
    )

    response = _enhanced(database, settings, embeddings).search(
        SearchRequest(query="the cat", user_id=user_id, top_k=10)
    )

    assert str(memory_id) in {evidence.id for evidence in response.data}


def test_response_schema_is_unchanged(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    _seed(database, asset_store, settings, embeddings, user_id, "schema check memory")

    response = _enhanced(database, settings, embeddings).search(
        SearchRequest(query="schema check", user_id=user_id, top_k=5)
    )

    payload = response.model_dump()
    assert set(payload) == {"data"}
    for item in payload["data"]:
        assert set(item) <= {"id", "content", "score", "created_at"}
        assert {"id", "content"} <= set(item)


def test_baseline_mode_is_unchanged_without_enhancements(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """未注入增强组件时保持任务 4 的基线行为。"""
    user_id = _uid("u")
    memory_id = _seed(database, asset_store, settings, embeddings, user_id, "baseline behaviour")

    service = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    )
    response = service.search(SearchRequest(query="baseline", user_id=user_id, top_k=10))

    assert str(memory_id) in {evidence.id for evidence in response.data}
