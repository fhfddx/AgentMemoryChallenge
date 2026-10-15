"""Real repository/Search integration: rendering consistency and cross-user edge rejection."""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from masm.api.app import create_app
from masm.config import RuntimeProfile, Settings
from masm.providers.fakes import DeterministicFakeEmbeddingProvider, FakeStructuredLLM
from masm.providers.reranker import LexicalReranker
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
from masm.retrieval.evidence_graph import EvidenceGraphPlanner, GraphEvidenceSelector
from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.relation_expander import RelationExpander
from masm.retrieval.relevance import RelevanceGate
from masm.retrieval.reranker import EvidenceReranker
from masm.retrieval.response_packer import ResponsePacker
from masm.retrieval.source_context import SourceContextEnricher
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.deletion_service import DeletionService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory, MemoryRelation
from masm.storage.repositories import MemoryRepository

_QUESTION = "What is the check-in code and where is the venue?"
_CODE_TEXT = "The check-in code is K7-42."
_VENUE_TEXT = "The venue is the old cinema."
_FOREIGN_TEXT = "The venue is the harbour office."


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _memory_id(database: Database, user_id: str, request_id: str) -> UUID:
    with database.session() as session:
        return session.execute(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.request_id == request_id,
                Memory.granularity == "context",
            )
        ).scalar_one()


def _seed(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
    user_id: str,
    text: str,
) -> tuple[str, UUID]:
    request_id = _uid("r")
    AddService(MemoryRepository(database), asset_store, settings, embeddings=embeddings).add(
        _request(request_id, user_id, text)
    )
    return request_id, _memory_id(database, user_id, request_id)


def _link(database: Database, user_id: str, source: UUID, target: UUID) -> None:
    """直接写入一条关系边（等价于 Add 阶段治理写入的持久化关系，可含损坏跨用户边）。"""
    with database.session() as session:
        session.add(
            MemoryRelation(
                user_id=user_id,
                source_type="memory",
                source_id=source,
                target_type="memory",
                target_id=target,
                relation_type="same_topic",
                confidence=1.0,
            )
        )
        session.commit()


class _PayloadPlannerLLM(FakeStructuredLLM):
    """确定性 planner 桩：引用包含指定标记的候选，可附带一条持久化关系链接。"""

    def __init__(
        self,
        *,
        markers: Sequence[str],
        requirements: Sequence[Mapping[str, object]],
        link: bool = False,
    ) -> None:
        super().__init__([], model="gpt-4o-mini")
        self._markers = list(markers)
        self._requirements = list(requirements)
        self._link = link
        self.payloads: list[Mapping[str, Any]] = []

    def complete_json(self, request: Any, output_type: Any) -> Any:
        if request.prompt_version == "evidence-graph-planner-v1":
            self.payloads.append(request.payload)
            self.queue(self._proposal(request.payload))
        return super().complete_json(request, output_type)

    def _proposal(self, payload: Mapping[str, Any]) -> dict[str, object]:
        candidates = list(payload["candidates"])
        texts: dict[str, str] = {}
        indices: dict[str, int] = {}
        for marker in self._markers:
            for candidate in candidates:
                text = str(candidate["text"])
                if marker in text:
                    texts[marker] = text
                    indices[marker] = int(candidate["index"])
                    break
            assert marker in indices, f"marker {marker!r} is absent from the candidate pool"
        supports = [
            {
                "requirement_index": index,
                "candidate_index": indices[marker],
                "candidate_quote": texts[marker],
                "role": "direct",
            }
            for index, marker in enumerate(self._markers)
        ]
        links: list[dict[str, object]] = []
        if self._link:
            links.append(
                {
                    "left_candidate_index": indices[self._markers[0]],
                    "right_candidate_index": indices[self._markers[1]],
                    "left_quote": texts[self._markers[0]],
                    "right_quote": texts[self._markers[1]],
                    "kind": "persisted_relation",
                }
            )
        return {"requirements": self._requirements, "supports": supports, "links": links}


def _requirements() -> list[dict[str, object]]:
    return [
        {"question_quote": "What is the check-in code", "kind": "attribute"},
        {"question_quote": "where is the venue", "kind": "location"},
    ]


def _service(
    repository: MemoryRepository,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
    llm: FakeStructuredLLM,
    *,
    graph: bool,
) -> SearchService:
    retriever = BaselineRetriever(repository, embeddings, DEFAULT_CHANNEL_WEIGHTS)
    selector = EvidenceSelector(llm)
    return SearchService(
        retriever,
        max_image_bytes=settings.max_image_bytes,
        expander=RelationExpander(repository),
        reranker=EvidenceReranker(provider=LexicalReranker()),
        relevance_gate=RelevanceGate(),
        selector=selector,
        graph_selector=(
            GraphEvidenceSelector(EvidenceGraphPlanner(llm), selector) if graph else None
        ),
        renderer=EvidenceRenderer(
            repository, asset_store, max_image_bytes=settings.max_image_bytes
        ),
        packer=ResponsePacker(),
        source_context=SourceContextEnricher(repository),
        runtime_profile="official-masm",
    )


def _cleanup(
    repository: MemoryRepository, asset_store: AssetStore, runs: Sequence[tuple[str, str]]
) -> None:
    for request_id, user_id in runs:
        assert DeletionService(repository, asset_store).delete_run(
            request_id, user_id=user_id
        ).complete


def _graph_payload(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    return json.loads(caplog.records[-1].message)


def test_persisted_relation_sidecar_enables_a_cross_source_chain(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = MemoryRepository(database)
    user = _uid("graph-owner")
    runs: list[tuple[str, str]] = []
    code_run, code_id = _seed(database, asset_store, settings, embeddings, user, _CODE_TEXT)
    venue_run, venue_id = _seed(
        database, asset_store, settings, embeddings, user, _VENUE_TEXT
    )
    runs.extend([(code_run, user), (venue_run, user)])
    _link(database, user, code_id, venue_id)
    try:
        assert [
            (edge.source_id, edge.target_id)
            for edge in repository.relation_edges(user, [code_id], 10)
        ] == [(code_id, venue_id)]

        llm = _PayloadPlannerLLM(
            markers=["K7-42", "old cinema"], requirements=_requirements(), link=True
        )
        service = _service(repository, asset_store, settings, embeddings, llm, graph=True)
        with caplog.at_level(logging.INFO, logger="masm.search"):
            response = service.search(SearchRequest(query=_QUESTION, user_id=user, top_k=10))

        assert [request.prompt_version for request in llm.requests] == [
            "evidence-graph-planner-v1"
        ]
        returned = {item.content for item in response.data}
        assert any(_CODE_TEXT in text for text in returned)
        assert any(_VENUE_TEXT in text for text in returned)

        # 渲染结果必须与 planner 实际看到的候选文本完全一致。
        planner_texts = {
            str(candidate["text"]) for candidate in llm.payloads[0]["candidates"]
        }
        assert returned <= planner_texts

        payload = _graph_payload(caplog)
        assert payload["graph_enabled"] is True
        assert payload["graph_planner_calls"] == 1
        assert payload["graph_link_count"] == 1
        assert payload["graph_state"] == "sufficient"
        assert payload["graph_fallback"] is False
    finally:
        _cleanup(repository, asset_store, runs)


def test_default_off_search_never_calls_the_planner(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = MemoryRepository(database)
    user = _uid("graph-off")
    run, _ = _seed(database, asset_store, settings, embeddings, user, _CODE_TEXT)
    try:
        llm = FakeStructuredLLM(
            [{"evidence_state": "sufficient", "selected_indices": [0]}],
            model="gpt-4o-mini",
        )
        with caplog.at_level(logging.INFO, logger="masm.search"):
            response = _service(
                repository, asset_store, settings, embeddings, llm, graph=False
            ).search(SearchRequest(query=_QUESTION, user_id=user, top_k=10))

        assert len(response.data) >= 1
        assert [request.prompt_version for request in llm.requests] == [
            "evidence-selector-v7"
        ]
        payload = _graph_payload(caplog)
        assert payload["graph_enabled"] is False
        assert payload["graph_planner_calls"] == 0
    finally:
        _cleanup(repository, asset_store, [(run, user)])


def test_cross_user_relation_edges_are_rejected(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
) -> None:
    repository = MemoryRepository(database)
    owner = _uid("edge-owner")
    stranger = _uid("edge-stranger")
    runs: list[tuple[str, str]] = []
    owner_run, owner_id = _seed(
        database, asset_store, settings, embeddings, owner, _CODE_TEXT
    )
    stranger_run, stranger_id = _seed(
        database, asset_store, settings, embeddings, stranger, _FOREIGN_TEXT
    )
    runs.extend([(owner_run, owner), (stranger_run, stranger)])
    # 以 owner 的 user_id 写入一条指向他人记忆的损坏边。
    _link(database, owner, owner_id, stranger_id)
    try:
        assert repository.relation_edges(owner, [owner_id], 10) == []
        assert repository.relation_edges(stranger, [stranger_id], 10) == []

        llm = _PayloadPlannerLLM(markers=["K7-42"], requirements=[_requirements()[0]])
        response = _service(
            repository, asset_store, settings, embeddings, llm, graph=True
        ).search(SearchRequest(query=_QUESTION, user_id=owner, top_k=10))

        returned = {item.content for item in response.data}
        assert not any(_FOREIGN_TEXT in text for text in returned)
        planner_payload = json.dumps(llm.payloads[0], ensure_ascii=False)
        assert _FOREIGN_TEXT not in planner_payload
        assert "harbour" not in planner_payload
    finally:
        _cleanup(repository, asset_store, runs)


def test_disconnected_cross_source_attributes_stay_partial(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = MemoryRepository(database)
    user = _uid("graph-plain")
    runs: list[tuple[str, str]] = []
    code_run, _ = _seed(database, asset_store, settings, embeddings, user, _CODE_TEXT)
    venue_run, _ = _seed(database, asset_store, settings, embeddings, user, _VENUE_TEXT)
    runs.extend([(code_run, user), (venue_run, user)])
    try:
        # 没有持久化关系边：模型声明的 persisted_relation 链接必须被拒绝。
        llm = _PayloadPlannerLLM(
            markers=["K7-42", "old cinema"], requirements=_requirements(), link=True
        )
        with caplog.at_level(logging.INFO, logger="masm.search"):
            response = _service(
                repository, asset_store, settings, embeddings, llm, graph=True
            ).search(SearchRequest(query=_QUESTION, user_id=user, top_k=10))

        payload = _graph_payload(caplog)
        assert payload["graph_link_count"] == 0
        assert payload["graph_state"] == "partial"
        returned = {item.content for item in response.data}
        assert not any(_VENUE_TEXT in text for text in returned)
    finally:
        _cleanup(repository, asset_store, runs)


def test_app_assembles_the_graph_selector_from_settings(
    database: Database,
    asset_store: AssetStore,
    settings: Settings,
    embeddings: DeterministicFakeEmbeddingProvider,
) -> None:
    def build(**overrides: object) -> Any:
        configured = replace(
            settings,
            runtime_profile=RuntimeProfile.OFFICIAL_MASM,
            llm_base_url="https://llm.example/v1",
            llm_api_key="llm-secret",
            embedding_base_url="https://embedding.example/v1",
            embedding_api_key="embedding-secret",
            **overrides,  # type: ignore[arg-type]
        )
        app = create_app(
            configured,
            database=database,
            asset_store=asset_store,
            embeddings=embeddings,
            llm=FakeStructuredLLM(model="gpt-4o-mini"),
        )
        return app.state.search_service

    default_service = build(source_context_enabled=True)
    assert default_service._graph_selector is None  # noqa: SLF001
    assert isinstance(default_service._selector, EvidenceSelector)  # noqa: SLF001

    graph_service = build(source_context_enabled=True, evidence_graph_enabled=True)
    assert isinstance(graph_service._graph_selector, GraphEvidenceSelector)  # noqa: SLF001
    assert isinstance(graph_service._selector, EvidenceSelector)  # noqa: SLF001

    with pytest.raises(ValueError):
        build(evidence_graph_enabled=True, source_context_enabled=False)
