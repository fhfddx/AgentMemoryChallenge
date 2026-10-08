"""SearchService query preparation tests."""

from masm.retrieval.baseline import ParsedQuery
from masm.schemas.api import SearchRequest
from masm.services.search_service import SearchService


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
