"""检索诊断只允许固定的聚合数字，不得带入身份或载荷。"""

import json
import logging
from uuid import uuid4

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.diagnostics import SearchDiagnostics, emit_search_diagnostics
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import EvidenceReranker
from masm.schemas.api import SearchRequest
from masm.services.search_service import SearchService
from masm.storage.types import MemoryCandidate


def _captured(caplog) -> dict:
    assert caplog.records, {
        "logger_disabled": logging.getLogger("masm.search").disabled,
        "logger_propagate": logging.getLogger("masm.search").propagate,
        "root_handlers": [type(handler).__name__ for handler in logging.getLogger().handlers],
        "capture_attached": caplog.handler in logging.getLogger().handlers,
        "global_disable": logging.root.manager.disable,
    }
    return json.loads(caplog.records[-1].message)


def test_diagnostics_never_logs_payload_or_identity(caplog) -> None:
    secrets = ("user-secret", "query-secret", "content-secret", "image-secret", "key-secret")
    diagnostic = SearchDiagnostics(
        request_tag="a" * 32,
        runtime_profile="official-masm",
        candidate_count=12,
        dedup_count=10,
        returned_count=5,
        response_bytes=1024,
        latency_ms=123.4,
        status_code=200,
        channel_counts={"lexical": 6, "query-secret": 9},
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        emit_search_diagnostics(diagnostic)

    log_text = caplog.text
    assert not any(secret in log_text for secret in secrets)
    payload = _captured(caplog)
    assert payload["candidate_count"] == 12
    assert payload["channel_counts"] == {"lexical": 6}
    assert set(payload) == {
        "request_tag", "runtime_profile", "candidate_count", "dedup_count",
        "returned_count", "response_bytes", "latency_ms", "status_code", "channel_counts",
        "selector_candidate_count", "selector_selected_count", "selector_source_count",
        "selector_selected_source_count", "selector_fallback", "selector_abstained",
        "selector_failure_category", "selector_latency_ms",
    }


def test_invalid_tag_and_profile_are_not_logged(caplog) -> None:
    diagnostic = SearchDiagnostics(
        request_tag="user-secret", runtime_profile="key-secret",
        candidate_count=0, dedup_count=0, returned_count=0,
        response_bytes=0, latency_ms=0.0, status_code=401, channel_counts={},
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        emit_search_diagnostics(diagnostic)

    assert "user-secret" not in caplog.text
    assert "key-secret" not in caplog.text
    payload = _captured(caplog)
    assert payload["runtime_profile"] == "unknown"
    assert len(payload["request_tag"]) == 32


def test_search_emits_aggregate_counts_without_query(caplog) -> None:
    class _Retriever:
        def retrieve_with_stats(self, user_id, query, limit):
            return [
                MemoryCandidate(
                    memory_id=uuid4(), user_id=user_id, content="query-secret",
                    score=1.0, granularity="message", request_id="run-1",
                    source_position=0,
                )
            ], {"lexical": 1}

    service = SearchService(
        _Retriever(), max_image_bytes=1024, reranker=EvidenceReranker(),
        runtime_profile="official-masm",
    )
    with caplog.at_level(logging.INFO, logger="masm.search"):
        response = service.search(
            SearchRequest(query="query-secret", user_id="user-secret", top_k=10)
        )

    assert len(response.data) == 1
    assert "query-secret" not in caplog.text
    assert "user-secret" not in caplog.text
    payload = _captured(caplog)
    assert payload["candidate_count"] == 1
    assert payload["returned_count"] == 1


def test_selector_diagnostics_expose_only_aggregate_outcome(caplog) -> None:
    class Retriever:
        def retrieve(self, user_id, query, limit):
            return [
                MemoryCandidate(
                    memory_id=uuid4(), user_id=user_id, content="private-content",
                    score=1.0, request_id="private-request", retrieval_signals={"lexical": 0.1},
                )
            ]

    service = SearchService(
        Retriever(), max_image_bytes=1024,
        selector=EvidenceSelector(FakeStructuredLLM([TimeoutError("private-error")])),
        runtime_profile="official-masm",
    )
    with caplog.at_level(logging.INFO, logger="masm.search"):
        response = service.search(
            SearchRequest(query="private-query", options=["private-option"],
                          user_id="private-user", top_k=10)
        )

    assert len(response.data) == 1
    for secret in (
        "private-content", "private-request", "private-error", "private-query",
        "private-option", "private-user",
    ):
        assert secret not in caplog.text
    payload = _captured(caplog)
    assert payload["selector_candidate_count"] == 1
    assert payload["selector_selected_count"] == 1
    assert payload["selector_source_count"] == 1
    assert payload["selector_selected_source_count"] == 1
    assert payload["selector_fallback"] is True
    assert payload["selector_abstained"] is False
    assert payload["selector_failure_category"] == "unavailable"
    assert payload["selector_latency_ms"] >= 0


def test_diagnostics_sanitize_unrecognized_selector_failure_category(caplog) -> None:
    diagnostic = SearchDiagnostics(
        request_tag="a" * 32, runtime_profile="official-masm", candidate_count=0,
        dedup_count=0, returned_count=0, response_bytes=0, latency_ms=0,
        status_code=200, channel_counts={}, selector_failure_category="private-error",
    )

    with caplog.at_level(logging.INFO, logger="masm.search"):
        emit_search_diagnostics(diagnostic)

    assert "private-error" not in caplog.text
    assert _captured(caplog)["selector_failure_category"] == "unknown"
