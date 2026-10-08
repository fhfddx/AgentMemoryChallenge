"""SearchService query preparation tests."""

from dataclasses import replace
from uuid import UUID

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.baseline import ParsedQuery
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.response_packer import ResponsePacker
from masm.schemas.api import SearchRequest
from masm.services.search_service import SearchService
from masm.storage.types import MemoryCandidate


class _RecordingRetriever:
    def __init__(self) -> None:
        self.query: ParsedQuery | None = None

    def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
        self.query = query
        return []


def test_search_options_do_not_enter_retrieval_text() -> None:
    retriever = _RecordingRetriever()
    service = SearchService(retriever, max_image_bytes=1024)

    response = service.search(
        SearchRequest(
            query="Where did Alice go?",
            options=["Paris", "Tokyo"],
            user_id="user-1",
            top_k=5,
        )
    )

    assert response.data == []
    assert retriever.query is not None
    assert retriever.query.text_queries == ("Where did Alice go?",)


def test_search_without_options_keeps_original_query() -> None:
    retriever = _RecordingRetriever()

    SearchService(retriever, max_image_bytes=1024).search(
        SearchRequest(query="plain question", user_id="user-1", top_k=5)
    )

    assert retriever.query == ParsedQuery(text_queries=("plain question",), intent="fact")


def test_selection_precedes_rendering_and_preserves_selected_order() -> None:
    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [
                MemoryCandidate(UUID(int=1), user_id, "first fact", 0.9, request_id="run-a"),
                MemoryCandidate(UUID(int=2), user_id, "second fact", 0.8, request_id="run-b"),
            ]

    class Renderer:
        def __init__(self) -> None:
            self.seen: list[UUID] = []

        def render(self, user_id: str, ranked):
            self.seen = [item.memory_id for item in ranked]
            return [replace(item, content=f"original {item.content}") for item in ranked]

    renderer = Renderer()
    llm = FakeStructuredLLM([{"selected_indices": [1, 0], "sufficient_evidence": True}])
    service = SearchService(
        Retriever(), max_image_bytes=1024, selector=EvidenceSelector(llm),
        renderer=renderer, packer=ResponsePacker(),
    )

    response = service.search(SearchRequest(query="facts", user_id="user-1", top_k=1))

    assert renderer.seen == [UUID(int=2), UUID(int=1)]
    assert [item.content for item in response.data] == ["original second fact"]


def test_insufficient_selection_is_successful_empty_response() -> None:
    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [MemoryCandidate(UUID(int=1), user_id, "Alice visited Paris", 0.9)]

    selector = EvidenceSelector(
        FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    )
    service = SearchService(Retriever(), max_image_bytes=1024, selector=selector)

    response = service.search(
        SearchRequest(query="What did Alice buy?", user_id="user-1", top_k=100)
    )

    assert response.model_dump() == {"data": []}
