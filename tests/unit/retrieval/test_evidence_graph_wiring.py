"""Opt-in wiring: default off, explicit dependencies, content-free graph telemetry."""

import json
import logging
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from masm.config import RuntimeProfile, Settings
from masm.providers.fakes import DeterministicFakeEmbeddingProvider, FakeStructuredLLM
from masm.retrieval.baseline import ParsedQuery
from masm.retrieval.diagnostics import SearchDiagnostics, emit_search_diagnostics
from masm.retrieval.evidence_graph import (
    EvidenceGraphPlanner,
    GraphEvidenceSelector,
    source_pairs_from_edges,
)
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.relation_expander import ExpansionEdge
from masm.retrieval.reranker import RankedEvidence
from masm.runtime import build_runtime
from masm.schemas.api import SearchRequest
from masm.services.search_service import SearchService
from masm.storage.types import MemoryCandidate

_OFFICIAL_FIELDS: dict[str, object] = {
    "database_url": "postgresql+psycopg://postgres@localhost/masm",
    "llm_base_url": "https://llm.example/v1",
    "llm_api_key": "llm-secret",
    "llm_model": "gpt-4o-mini",
    "embedding_base_url": "https://embedding.example/v1",
    "embedding_api_key": "embedding-secret",
    "embedding_model": "text-embedding-v4",
    "embedding_dimensions": 1024,
    "model_timeout_seconds": 30.0,
    "model_max_attempts": 2,
}


def _official(profile: RuntimeProfile = RuntimeProfile.OFFICIAL_MASM) -> Settings:
    return Settings(runtime_profile=profile, **_OFFICIAL_FIELDS)  # type: ignore[arg-type]


def _runtime_llm() -> FakeStructuredLLM:
    return FakeStructuredLLM(model="gpt-4o-mini")


def _graph_settings(**overrides: object) -> Settings:
    base = replace(
        _official(),
        selector_enabled=True,
        source_context_enabled=True,
        evidence_graph_enabled=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# --- configuration -----------------------------------------------------------------------


def test_graph_flag_is_off_unless_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MASM_EVIDENCE_GRAPH_ENABLED", raising=False)
    assert Settings.from_env().evidence_graph_enabled is False
    monkeypatch.setenv("MASM_EVIDENCE_GRAPH_ENABLED", "1")
    assert Settings.from_env().evidence_graph_enabled is True
    monkeypatch.setenv("MASM_EVIDENCE_GRAPH_ENABLED", "true")
    assert Settings.from_env().evidence_graph_enabled is False


def test_graph_caps_and_timeout_are_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (
        ("MASM_EVIDENCE_GRAPH_ENABLED", "1"),
        ("MASM_EVIDENCE_GRAPH_MAX_REQUIREMENTS", "3"),
        ("MASM_EVIDENCE_GRAPH_MAX_SUPPORTS", "12"),
        ("MASM_EVIDENCE_GRAPH_MAX_LINKS", "9"),
        ("MASM_EVIDENCE_GRAPH_TIMEOUT_SECONDS", "12.5"),
    ):
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.evidence_graph_max_requirements == 3
    assert settings.evidence_graph_max_supports == 12
    assert settings.evidence_graph_max_links == 9
    assert settings.evidence_graph_timeout_seconds == 12.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"selector_enabled": False},
        {"source_context_enabled": False},
        {"runtime_profile": RuntimeProfile.LOCAL_FAKE},
        {"runtime_profile": RuntimeProfile.OFFICIAL_BASELINE},
    ],
)
def test_graph_mode_rejects_missing_dependencies_at_startup(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _graph_settings(**overrides).validate_runtime()


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_graph_max_requirements": 0},
        {"evidence_graph_max_requirements": 5},
        {"evidence_graph_max_supports": 0},
        {"evidence_graph_max_supports": 25},
        {"evidence_graph_max_links": 0},
        {"evidence_graph_max_links": 25},
        {"evidence_graph_timeout_seconds": 0.0},
        {"evidence_graph_timeout_seconds": 31.0},
    ],
)
def test_graph_caps_cannot_exceed_hard_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _graph_settings(**overrides).validate_runtime()


def test_graph_settings_validate_when_dependencies_are_satisfied() -> None:
    _graph_settings().validate_runtime()


# --- runtime assembly --------------------------------------------------------------------


def test_runtime_omits_the_graph_selector_by_default() -> None:
    runtime = build_runtime(
        replace(_official(), source_context_enabled=True),
        repository=_mock_repository(),
        embeddings=DeterministicFakeEmbeddingProvider(),
        llm=_runtime_llm(),
    )

    assert runtime.evidence_graph is None
    assert isinstance(runtime.evidence_selector, EvidenceSelector)


def test_runtime_assembles_the_graph_selector_only_when_enabled() -> None:
    runtime = build_runtime(
        _graph_settings(),
        repository=_mock_repository(),
        embeddings=DeterministicFakeEmbeddingProvider(),
        llm=_runtime_llm(),
    )

    assert isinstance(runtime.evidence_graph, GraphEvidenceSelector)
    # 回退依赖必须仍是同一个冻结的 v7 selector。
    assert isinstance(runtime.evidence_selector, EvidenceSelector)


def _mock_repository() -> object:
    from unittest.mock import Mock

    from masm.storage.repositories import MemoryRepository

    return Mock(spec=MemoryRepository)


# --- search wiring -----------------------------------------------------------------------


def _evidence(number: int, source: str, content: str, *, position: int = 0) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id="user-1",
        content=content,
        score=1.0 / number,
        rank=number,
        request_id=source,
        granularity="message",
        source_position=position,
        metadata={"lexical": 0.05},
    )


class _Retriever:
    def __init__(self, items: list[RankedEvidence]) -> None:
        self.items = items

    def retrieve(self, user_id: str, query: ParsedQuery, limit: int) -> list[MemoryCandidate]:
        del query, limit
        return [
            MemoryCandidate(
                memory_id=item.memory_id,
                user_id=user_id,
                content=str(item.content),
                score=item.score,
                request_id=item.request_id,
                source_position=item.source_position,
                granularity=item.granularity,
                retrieval_signals={"lexical": 0.05},
            )
            for item in self.items
        ]


def _proposal() -> dict[str, object]:
    return {
        "requirements": [
            {"question_quote": "Where did I store the amber key", "kind": "location"}
        ],
        "supports": [
            {
                "requirement_index": 0,
                "candidate_index": 0,
                "candidate_quote": "I stored the amber key in drawer three.",
                "role": "direct",
            }
        ],
        "links": [],
    }


def test_search_without_graph_mode_never_calls_a_planner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = FakeStructuredLLM([{"evidence_state": "sufficient", "selected_indices": [0]}])
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    service = SearchService(
        _Retriever([direct]),
        max_image_bytes=1024,
        selector=EvidenceSelector(llm),
        runtime_profile="official-masm",
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        response = service.search(
            SearchRequest(query="Where did I store the amber key?", user_id="user-1", top_k=10)
        )

    assert len(response.data) == 1
    assert [request.prompt_version for request in llm.requests] == ["evidence-selector-v7"]
    payload = json.loads(caplog.records[-1].message)
    assert payload["graph_enabled"] is False
    assert payload["graph_planner_calls"] == 0
    assert payload["graph_state"] == "unknown"


def test_search_with_graph_mode_issues_exactly_one_planner_call() -> None:
    llm = FakeStructuredLLM([_proposal()])
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    service = SearchService(
        _Retriever([direct]),
        max_image_bytes=1024,
        selector=EvidenceSelector(llm),
        graph_selector=GraphEvidenceSelector(
            EvidenceGraphPlanner(llm), EvidenceSelector(llm), additive=False
        ),
        runtime_profile="official-masm",
    )

    response = service.search(
        SearchRequest(query="Where did I store the amber key?", user_id="user-1", top_k=10)
    )

    assert [request.prompt_version for request in llm.requests] == [
        "evidence-graph-planner-v2"
    ]
    assert [item.content for item in response.data] == [
        "I stored the amber key in drawer three."
    ]


def test_graph_diagnostics_are_content_free(caplog: pytest.LogCaptureFixture) -> None:
    llm = FakeStructuredLLM([_proposal()])
    secret = _evidence(1, "request-secret", "I stored the amber key in drawer three.")
    service = SearchService(
        _Retriever([secret]),
        max_image_bytes=1024,
        selector=EvidenceSelector(llm),
        graph_selector=GraphEvidenceSelector(
            EvidenceGraphPlanner(llm), EvidenceSelector(llm), additive=False
        ),
        runtime_profile="official-masm",
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        service.search(
            SearchRequest(
                query="Where did I store the amber key?",
                user_id="private-user",
                top_k=10,
            )
        )

    payload = json.loads(caplog.records[-1].message)
    rendered = caplog.text
    for leak in ("amber key", "drawer three", "request-secret", "private-user"):
        assert leak not in rendered
    assert payload["graph_enabled"] is True
    assert payload["graph_planner_calls"] == 1
    assert payload["graph_requirement_count"] == 1
    assert payload["graph_support_count"] == 1
    assert payload["graph_link_count"] == 0
    assert payload["graph_selected_count"] == 1
    assert payload["graph_state"] == "sufficient"
    assert payload["graph_fallback"] is False
    assert payload["graph_failure_category"] == "none"
    assert payload["graph_planner_latency_ms"] >= 0


def test_graph_diagnostics_sanitize_unrecognized_enums(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic = SearchDiagnostics(
        request_tag="a" * 32,
        runtime_profile="official-masm",
        candidate_count=0,
        dedup_count=0,
        returned_count=0,
        response_bytes=0,
        latency_ms=0.0,
        status_code=200,
        channel_counts={},
        graph_state="private-content-state",
        graph_failure_category="private-error",
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        emit_search_diagnostics(diagnostic)

    payload = json.loads(caplog.records[-1].message)
    assert payload["graph_state"] == "unknown"
    assert payload["graph_failure_category"] == "unknown"
    assert "private-content-state" not in caplog.text
    assert "private-error" not in caplog.text


# --- graph context isolation -------------------------------------------------------------


def test_source_pairs_ignore_cross_user_and_unknown_endpoints() -> None:
    mine = MemoryCandidate(
        memory_id=UUID(int=1), user_id="user-1", content="a", score=1.0, request_id="source-a"
    )
    other = MemoryCandidate(
        memory_id=UUID(int=2), user_id="other-user", content="b", score=1.0, request_id="source-b"
    )
    edges = (
        ExpansionEdge(UUID(int=1), UUID(int=2), "follows", 1),
        ExpansionEdge(UUID(int=1), UUID(int=42), "follows", 1),
    )

    assert source_pairs_from_edges(edges, [mine, other], "user-1") == ()


def test_source_pairs_are_deduplicated_and_sorted() -> None:
    first = MemoryCandidate(
        memory_id=UUID(int=1), user_id="user-1", content="a", score=1.0, request_id="source-b"
    )
    second = MemoryCandidate(
        memory_id=UUID(int=2), user_id="user-1", content="b", score=1.0, request_id="source-a"
    )
    edges = (
        ExpansionEdge(UUID(int=1), UUID(int=2), "follows", 1),
        ExpansionEdge(UUID(int=2), UUID(int=1), "follows", 2),
    )

    assert source_pairs_from_edges(edges, [first, second], "user-1") == (
        ("source-a", "source-b"),
    )


def test_unknown_request_ids_never_become_provenance() -> None:
    item = MemoryCandidate(
        memory_id=uuid4(), user_id="user-1", content="a", score=1.0, request_id=""
    )
    edges = (ExpansionEdge(item.memory_id, item.memory_id, "follows", 1),)

    assert source_pairs_from_edges(edges, [item], "user-1") == ()
