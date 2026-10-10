"""增强 Search 集成测试：真实 Repository 链路下的扩展、冲突补全与重复惩罚。"""

import base64
import io
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import delete, select, update

from masm.api.app import create_app
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.reranker import RerankerProvider
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever, ParsedQuery
from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.query_analyzer import QueryAnalyzer
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.relevance import RelevanceGate
from masm.retrieval.reranker import MAX_RERANK_CANDIDATES, EvidenceReranker
from masm.retrieval.response_packer import ResponsePacker
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryEmbedding, MemoryRelation
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _png_data_url() -> tuple[bytes, str]:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), (15, 25, 35)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    return payload, f"data:image/png;base64,{base64.b64encode(payload).decode()}"


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
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
                Memory.granularity == "context",
            )
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
    with database.session() as session:
        neighbour_run = session.get(Memory, neighbour_id).request_id
        neighbour_message = session.execute(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.request_id == neighbour_run,
                Memory.granularity == "message",
            )
        ).scalar_one()
    assert str(neighbour_message) in {evidence.id for evidence in response.data}


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

    with database.session() as session:
        request_id = session.get(Memory, memory_id).request_id
        message_id = session.execute(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
                Memory.granularity == "message",
            )
        ).scalar_one()
    assert str(message_id) in {evidence.id for evidence in response.data}


def test_search_options_do_not_change_recall_query() -> None:
    class RecordingRetriever:
        def __init__(self) -> None:
            self.queries: list[ParsedQuery] = []

        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            self.queries.append(query)
            return []

    retriever = RecordingRetriever()
    service = SearchService(retriever, max_image_bytes=1024)

    service.search(
        SearchRequest(query="What did Alice buy?", options=["Paris"], user_id="u", top_k=10)
    )
    service.search(
        SearchRequest(query="What did Alice buy?", options=["Books"], user_id="u", top_k=10)
    )

    assert len(retriever.queries) == 2
    assert retriever.queries[0].text_queries == ("What did Alice buy?",)
    assert retriever.queries[1].text_queries == retriever.queries[0].text_queries


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


def test_context_and_user_isolation(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """旧 context 和新 message 均可召回；按运行补候选不能跨用户。"""
    user_id = _uid("u")
    other_user = _uid("u")
    request_id = _uid("same-run")
    service = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    service.add(_request(request_id, user_id, "needle private fact"))
    service.add(_request(request_id, other_user, "needle other user"))
    repo = MemoryRepository(database)

    lexical = repo.lexical_candidates(user_id, "needle", 10)
    contexts = repo.context_candidates_for_requests(user_id, [request_id])
    messages = repo.message_candidates_for_requests(user_id, [request_id], 10)

    assert {row.granularity for row in lexical} == {"context", "message"}
    assert len(contexts) == len(messages) == 1
    assert all(row.user_id == user_id for row in [*lexical, *contexts, *messages])
    assert contexts[0].source_position is None
    assert messages[0].source_position == 0
    assert all(row.request_id == request_id for row in [*contexts, *messages])


def test_legacy_context_without_messages_remains_searchable(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    context_id = _seed(database, asset_store, settings, embeddings, user_id, "legacy needle")
    with database.session() as session:
        request_id = session.get(Memory, context_id).request_id
        session.execute(
            delete(Memory).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
                Memory.granularity == "message",
            )
        )
        session.commit()

    response = _enhanced(database, settings, embeddings).search(
        SearchRequest(query="legacy needle", user_id=user_id, top_k=10)
    )

    assert str(context_id) in {item.id for item in response.data}


def test_parent_child_dedup_preserves_relation_anchor(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """消息命中时仍由父 context 触发跨运行一跳关系，并补入邻居消息。"""
    user_id = _uid("u")
    first = _seed(database, asset_store, settings, embeddings, user_id, "anchor needle")
    second = _seed(database, asset_store, settings, embeddings, user_id, "neighbour fact")
    _add_relation(database, user_id, first, second)
    with database.session() as session:
        session.execute(update(Memory).where(Memory.id == first).values(summary="hidden parent"))
        session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == first))
        first_run = session.get(Memory, first).request_id
        second_run = session.get(Memory, second).request_id
        message_ids = {
            row.request_id: row.id
            for row in session.execute(
                select(Memory).where(
                    Memory.user_id == user_id,
                    Memory.granularity == "message",
                    Memory.request_id.in_([first_run, second_run]),
                )
            ).scalars()
        }
        session.commit()

    initial_ids = {
        row.memory_id
        for row in BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ).retrieve(user_id, _parsed("anchor needle"), 10)
    }
    assert first not in initial_ids

    repository = MemoryRepository(database)

    class _MessageOnlyRetriever:
        def __init__(self) -> None:
            self.repository = repository

        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [
                MemoryCandidate(
                    memory_id=message_ids[first_run], user_id=user_id,
                    content="anchor needle", score=0.8, granularity="message",
                    request_id=first_run, source_position=0,
                )
            ]

    response = SearchService(
        _MessageOnlyRetriever(), max_image_bytes=settings.max_image_bytes,
        expander=RelationExpander(repository), reranker=EvidenceReranker(),
    ).search(SearchRequest(query="anchor needle", user_id=user_id, top_k=10))
    ids = [row.id for row in response.data]

    assert str(message_ids[first_run]) in ids
    assert str(message_ids[second_run]) in ids
    assert len(ids) == len(set(ids))


def test_top_100_with_parent_child_pool() -> None:
    """Search 必须扩大原始池，同时将 Provider 输入限制为 64。"""
    class _Retriever:
        def __init__(self) -> None:
            self.requested_limit = 0
            self.candidates = [
                MemoryCandidate(
                    memory_id=uuid4(), user_id="test-user", content="needle " + "x" * 100000,
                    score=0.5, request_id="run-1",
                ),
                *[
                    MemoryCandidate(
                        memory_id=uuid4(), user_id="test-user", content=f"needle fact {index}",
                        score=0.5, granularity="message", request_id=f"run-{index // 2}",
                        source_position=index % 2,
                    )
                    for index in range(110)
                ],
            ]

        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            self.requested_limit = limit
            return self.candidates[:limit]

    class _Provider(RerankerProvider):
        model_name = "recording"

        def __init__(self) -> None:
            self.input_count = 0

        def score(self, query: str, documents) -> list[float]:
            self.input_count = len(documents)
            return [0.0] * len(documents)

    retriever = _Retriever()
    provider = _Provider()
    service = SearchService(
        retriever, max_image_bytes=1024,
        reranker=EvidenceReranker(provider=provider), packer=ResponsePacker(),
    )

    response = service.search(SearchRequest(query="needle", user_id="test-user", top_k=100))

    assert retriever.requested_limit > 100
    assert len(response.data) == 100
    assert response.data[0].content.startswith("needle fact")
    assert provider.input_count <= 64
    assert len(response.model_dump_json().encode("utf-8")) <= 30 * 1024 * 1024


def test_baseline_top_100_preserves_all_message_positions() -> None:
    """即便没有官方重排器，同源长上下文也不应占用最后一个消息席位。"""
    class _Retriever:
        requested_limit = 0

        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            self.requested_limit = limit
            return [
                MemoryCandidate(
                    memory_id=UUID(int=0), user_id=user_id, content="needle parent",
                    score=0.5, request_id="run-1",
                ),
                *[
                    MemoryCandidate(
                        memory_id=UUID(int=index + 1), user_id=user_id,
                        content=f"needle fact {index}", score=0.5,
                        granularity="message", request_id="run-1", source_position=index,
                    )
                    for index in range(100)
                ],
            ][:limit]

    retriever = _Retriever()
    response = SearchService(retriever, max_image_bytes=1024).search(
        SearchRequest(query="needle", user_id="test-user", top_k=100)
    )

    assert retriever.requested_limit > 100
    assert {row.id for row in response.data} == {str(UUID(int=index + 1)) for index in range(100)}


def test_same_source_content_is_not_returned_twice() -> None:
    class _Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [
                MemoryCandidate(
                    memory_id=uuid4(), user_id=user_id, content="  needle   fact  ",
                    score=0.9, request_id="run-1",
                ),
                MemoryCandidate(
                    memory_id=uuid4(), user_id=user_id, content="Needle Fact",
                    score=0.8, granularity="message", request_id="run-1",
                    source_position=0,
                ),
            ]

    response = SearchService(
        _Retriever(), max_image_bytes=1024, reranker=EvidenceReranker(),
    ).search(SearchRequest(query="needle", user_id="user-1", top_k=10))

    assert len(response.data) == 1
    assert response.data[0].content == "Needle Fact"


class _SignalRetriever:
    def __init__(self, candidate: MemoryCandidate) -> None:
        self.candidate = candidate

    def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
        return [self.candidate]


class _RecordingExpander:
    max_seeds = 8

    def __init__(self, neighbour: MemoryCandidate) -> None:
        self.neighbour = neighbour
        self.calls: list[list[UUID]] = []

    def expand(self, user_id: str, seeds, limit: int):
        self.calls.append([seed.memory_id for seed in seeds])
        return [self.neighbour]


def test_weak_nearest_neighbour_abstains_before_relation_expansion() -> None:
    weak = MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content="unrelated history", score=0.02,
        retrieval_signals={"text_vector": 0.2},
    )
    neighbour = MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content="related only by edge", score=0.0,
    )
    expander = _RecordingExpander(neighbour)

    response = SearchService(
        _SignalRetriever(weak), max_image_bytes=1024,
        relevance_gate=RelevanceGate(), expander=expander, reranker=EvidenceReranker(),
    ).search(SearchRequest(query="unknown topic", user_id="user-1", top_k=10))

    assert response.data == []
    assert expander.calls == []


def test_admitted_anchor_keeps_relation_expanded_evidence() -> None:
    anchor = MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content="exact anchor", score=0.02,
        retrieval_signals={"lexical": 0.01},
    )
    neighbour = MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content="cross-session evidence", score=0.0,
    )
    expander = _RecordingExpander(neighbour)

    response = SearchService(
        _SignalRetriever(anchor), max_image_bytes=1024,
        relevance_gate=RelevanceGate(), expander=expander, reranker=EvidenceReranker(),
    ).search(SearchRequest(query="exact anchor", user_id="user-1", top_k=10))

    assert expander.calls == [[anchor.memory_id]]
    assert {item.id for item in response.data} == {
        str(anchor.memory_id), str(neighbour.memory_id),
    }


def test_source_snapshots_are_batched_deduplicated_and_user_scoped(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    other_user = _uid("u")
    request_id = _uid("source")
    _image_bytes, image_url = _png_data_url()
    add = AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings)
    add.add(
        AddRequest(
            request_id=request_id,
            user_id=user_id,
            session_id="session-source",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": "after"},
                    ],
                }
            ],
        )
    )

    repository = MemoryRepository(database)
    snapshots = repository.source_messages_for_positions(
        user_id,
        [(request_id, 0), (request_id, 0), ("missing", 0)],
    )

    assert list(snapshots) == [(request_id, 0)]
    snapshot = snapshots[(request_id, 0)]
    assert [part["type"] for part in snapshot.content] == ["text", "image_url", "text"]
    object_uri = snapshot.content[1]["image_url"]["url"]
    assert object_uri in snapshot.assets
    assert snapshot.assets[object_uri].media_type == "image/png"
    assert repository.source_messages_for_positions(other_user, [(request_id, 0)]) == {}


def test_application_search_returns_original_ordered_multimodal_message(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    request_id = _uid("render")
    image_bytes, image_url = _png_data_url()
    app = create_app(
        settings,
        database=database,
        asset_store=asset_store,
        embeddings=embeddings,
    )
    app.state.add_service.add(
        AddRequest(
            request_id=request_id,
            user_id=user_id,
            session_id="session-render",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "visual needle before"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": "visual needle after"},
                    ],
                }
            ],
        )
    )

    response = app.state.search_service.search(
        SearchRequest(query="visual needle", user_id=user_id, top_k=2)
    )

    content = next(item.content for item in response.data if isinstance(item.content, list))
    assert isinstance(content, list)
    assert [part.type for part in content] == ["text", "image_url", "text"]
    assert content[0].text == "visual needle before"
    assert base64.b64decode(content[1].image_url.url.split(",", 1)[1]) == image_bytes
    assert content[2].text == "visual needle after"


def test_selector_can_return_original_facts_from_two_add_requests(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    _seed(database, asset_store, settings, embeddings, user_id, "Alice adopted Nimbus")
    _seed(database, asset_store, settings, embeddings, user_id, "Nimbus sleeps greenhouse")
    repository = MemoryRepository(database)
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    service = SearchService(
        BaselineRetriever(repository, embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
        analyzer=QueryAnalyzer(),
        relevance_gate=RelevanceGate(),
        reranker=EvidenceReranker(),
        selector=EvidenceSelector(llm),
        renderer=EvidenceRenderer(repository, asset_store, max_image_bytes=1024),
        packer=ResponsePacker(),
    )
    request = SearchRequest(
        query="Alice adopted Nimbus; Nimbus sleeps greenhouse",
        options=["Rover", "Nimbus"], user_id=user_id, top_k=10,
    )

    assert service.search(request).data == []
    candidates = llm.requests[-1].payload["candidates"]
    chosen = [
        candidate["index"] for candidate in candidates
        if candidate["granularity"] == "message"
        and candidate["text"] in {"Alice adopted Nimbus", "Nimbus sleeps greenhouse"}
    ]
    assert len(chosen) == 2
    llm.queue({"selected_indices": chosen, "sufficient_evidence": True})

    response = service.search(request)

    assert {item.content for item in response.data} == {
        "Alice adopted Nimbus", "Nimbus sleeps greenhouse"
    }


def test_selector_restores_original_multimodal_message_after_selection(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    user_id = _uid("u")
    image_bytes, image_url = _png_data_url()
    app = create_app(
        settings, database=database, asset_store=asset_store, embeddings=embeddings
    )
    app.state.add_service.add(
        AddRequest(
            request_id=_uid("r"), user_id=user_id, session_id="session-image",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "visual needle before"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": "visual needle after"},
                ],
            }],
        )
    )
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    app.state.search_service._selector = EvidenceSelector(llm)  # noqa: SLF001
    request = SearchRequest(query="visual needle", user_id=user_id, top_k=10)

    assert app.state.search_service.search(request).data == []
    candidates = llm.requests[-1].payload["candidates"]
    message_indices = [
        item["index"] for item in candidates if item["granularity"] == "message"
    ]
    assert len(message_indices) == 1
    llm.queue({"selected_indices": message_indices, "sufficient_evidence": True})

    response = app.state.search_service.search(request)

    assert len(response.data) == 1
    content = response.data[0].content
    assert isinstance(content, list)
    assert [part.type for part in content] == ["text", "image_url", "text"]
    assert base64.b64decode(content[1].image_url.url.split(",", 1)[1]) == image_bytes

    # No text describes the image query to the selector, so retain admitted
    # image hits without sending an uninformed selection request.
    requests_before = len(llm.requests)
    pure_visual = app.state.search_service.search(
        SearchRequest(
            query=[{"type": "image_url", "image_url": {"url": image_url}}],
            user_id=user_id,
            top_k=10,
        )
    )
    assert any(isinstance(item.content, list) for item in pure_visual.data)
    assert len(llm.requests) == requests_before
