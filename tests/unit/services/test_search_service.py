"""SearchService query preparation tests."""

from dataclasses import replace
from uuid import UUID

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.baseline import ParsedQuery
from masm.retrieval.evidence_graph import GraphDiagnostics
from masm.retrieval.evidence_selector import EvidenceSelector, SelectionResult
from masm.retrieval.reranker import RankedEvidence
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


def test_refusal_gate_returns_empty_before_the_selector() -> None:
    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [MemoryCandidate(UUID(int=1), user_id, "Alice visited Paris", 0.9)]

    seen: list[tuple[str, str]] = []

    def gate(user_id: str, question: str) -> bool:
        seen.append((user_id, question))
        return True

    service = SearchService(Retriever(), max_image_bytes=1024, refusal_gate=gate)

    response = service.search(
        SearchRequest(query="What does my sister Nellwyn do for work?", user_id="user-1", top_k=5)
    )

    assert response.model_dump() == {"data": []}
    assert seen == [("user-1", "What does my sister Nellwyn do for work?")]


def test_refusal_gate_that_declines_keeps_the_retrieved_evidence() -> None:
    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [MemoryCandidate(UUID(int=1), user_id, "Alice visited Paris", 0.9)]

    service = SearchService(
        Retriever(), max_image_bytes=1024, refusal_gate=lambda user_id, question: False
    )

    response = service.search(
        SearchRequest(query="Where did Alice go?", user_id="user-1", top_k=5)
    )

    assert [item.content for item in response.data] == ["Alice visited Paris"]


def test_refusal_gate_is_not_consulted_for_multimodal_queries() -> None:
    calls: list[str] = []

    def gate(user_id: str, question: str) -> bool:
        calls.append(question)
        return True

    service = SearchService(_RecordingRetriever(), max_image_bytes=1024, refusal_gate=gate)

    response = service.search(
        SearchRequest(
            query=[{"type": "text", "text": "What does my sister Nellwyn do?"}],
            user_id="user-1",
            top_k=5,
        )
    )

    assert response.data == []
    assert calls == []


class _GraphOutcome:
    def __init__(self, selection, graph) -> None:
        self.selection = selection
        self.graph = graph


class _EmptyThenEvidenceGraphSelector:
    """第一次返回空选择，补召回之后再返回证据；记录每次调用看到的候选数。"""

    def __init__(self, retry_evidence: UUID) -> None:
        self._retry_evidence = retry_evidence
        self.pool_sizes: list[int] = []

    def select(self, query, options, ranked, anchors, context, *, request_user_id):
        del query, options, anchors, context, request_user_id
        self.pool_sizes.append(len(ranked))
        if len(self.pool_sizes) == 1:
            return _GraphOutcome(
                SelectionResult(
                    evidence=(),
                    candidate_count=len(ranked),
                    source_count=0,
                    selected_source_count=0,
                    fallback=False,
                    abstained=True,
                    failure_category="none",
                    evidence_state="insufficient",
                ),
                GraphDiagnostics(enabled=True, candidate_count=len(ranked), selected_count=0),
            )
        chosen = next(item for item in ranked if item.memory_id == self._retry_evidence)
        return _GraphOutcome(
            SelectionResult(
                evidence=(chosen,),
                candidate_count=len(ranked),
                source_count=1,
                selected_source_count=1,
                fallback=False,
                abstained=False,
                failure_category="none",
                evidence_state="sufficient",
            ),
            GraphDiagnostics(enabled=True, candidate_count=len(ranked), selected_count=1),
        )


def test_empty_selection_retries_with_supplementary_recall() -> None:
    cross_session = UUID(int=99)

    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [MemoryCandidate(UUID(int=1), user_id, "Priya approved the trip.", 0.9)]

    selector = _EmptyThenEvidenceGraphSelector(cross_session)
    seen: list[tuple[str, list[str], int]] = []

    def recall(user_id: str, units, limit: int):
        seen.append((user_id, list(units), limit))
        return [
            RankedEvidence(
                memory_id=cross_session,
                user_id=user_id,
                content="The Spanish offsite is confirmed for the week of 10 November.",
                score=0.5,
                rank=1,
            )
        ]

    service = SearchService(
        Retriever(),
        max_image_bytes=1024,
        graph_selector=selector,
        supplementary_recall=recall,
    )

    response = service.search(
        SearchRequest(
            query="When is the Barcelona offsite confirmed for?",
            user_id="user-1",
            top_k=5,
        )
    )

    # 第一次只有原池（1 条），补救之后是原池 + 补召回（2 条）。
    assert selector.pool_sizes == [1, 2]
    assert [item.id for item in response.data] == [str(cross_session)]
    assert seen and seen[0][0] == "user-1"


def test_answered_search_never_pays_for_the_retry() -> None:
    class Retriever:
        def retrieve(self, user_id: str, query: ParsedQuery, limit: int):
            return [MemoryCandidate(UUID(int=1), user_id, "Alice visited Paris.", 0.9)]

    class Selector:
        def __init__(self) -> None:
            self.calls = 0

        def select(self, query, options, ranked, anchors, context, *, request_user_id):
            del query, options, anchors, context, request_user_id
            self.calls += 1
            return _GraphOutcome(
                SelectionResult(
                    evidence=(ranked[0],),
                    candidate_count=len(ranked),
                    source_count=1,
                    selected_source_count=1,
                    fallback=False,
                    abstained=False,
                    failure_category="none",
                    evidence_state="sufficient",
                ),
                GraphDiagnostics(enabled=True, candidate_count=len(ranked), selected_count=1),
            )

    def recall(user_id: str, units, limit: int):  # pragma: no cover - 不应被调用
        raise AssertionError("supplementary recall must not run when evidence was selected")

    selector = Selector()
    service = SearchService(
        Retriever(),
        max_image_bytes=1024,
        graph_selector=selector,
        supplementary_recall=recall,
    )

    service.search(SearchRequest(query="Where did Alice go?", user_id="user-1", top_k=5))

    assert selector.calls == 1
