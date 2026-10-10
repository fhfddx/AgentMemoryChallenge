"""The multi-session probe exercises a synthetic chain and cleans every run."""

import importlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from io import StringIO
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from masm.services.deletion_service import DeletionReport


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.payload


class _InMemoryBackend:
    def __init__(
        self,
        *,
        run_tag: str,
        fail_on_search: int | None = None,
        leak_disconnected: bool = False,
        link_disconnected_graph: bool = False,
        return_disconnected_evidence: bool = False,
    ) -> None:
        self.run_tag = run_tag
        self.fail_on_search = fail_on_search
        self.leak_disconnected = leak_disconnected
        self.link_disconnected_graph = link_disconnected_graph
        self.return_disconnected_evidence = return_disconnected_evidence
        self.runs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._search_index = 0

    def post(self, path: str, *, json: dict[str, Any]) -> _Response:
        self.requests.append((path, json))
        if path == "/add":
            self.runs[json["request_id"]] = json
            return _Response(
                200,
                {
                    "success": True,
                    "request_id": json["request_id"],
                    "user_id": json["user_id"],
                    "session_id": json["session_id"],
                },
            )
        assert path == "/search"
        if self._search_index == self.fail_on_search:
            self._search_index += 1
            return _Response(503, {"detail": "private-provider-body"})

        root = f"Root-{self.run_tag}"
        bridge = f"Bridge-{self.run_tag}"
        leaf = f"Leaf-{self.run_tag}"
        terminal = f"Terminal-{self.run_tag}"
        connected = [
            {"id": "chain-1", "content": f"{root} points through {bridge}."},
            {"id": "chain-2", "content": f"{bridge} points through {leaf}."},
            {"id": "chain-3", "content": f"{leaf} ends at {terminal}."},
        ]
        if self.leak_disconnected:
            connected.append(
                {
                    "id": "disconnected",
                    "content": f"Unconnected-{self.run_tag} is only a lookalike.",
                }
            )
        disconnected = (
            [
                {
                    "id": "disconnected",
                    "content": f"Unconnected-{self.run_tag} is only a lookalike.",
                }
            ]
            if self.return_disconnected_evidence
            else []
        )
        searches: tuple[dict[str, Any], ...] = (
            {
                "data": connected
            },
            {"data": disconnected},
        )
        payload = searches[self._search_index]
        self._search_index += 1
        return _Response(200, payload)

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[SimpleNamespace]:
        return self._candidates(user_id, request_ids)

    def message_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str], limit: int
    ) -> list[SimpleNamespace]:
        return self._candidates(user_id, request_ids)[:limit]

    def _candidates(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                content=self.runs[run_id]["messages"][0]["content"],
                memory_id=uuid5(NAMESPACE_URL, run_id),
                request_id=run_id,
            )
            for run_id in request_ids
            if run_id in self.runs and self.runs[run_id]["user_id"] == user_id
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[Any], limit: int
    ) -> list[SimpleNamespace]:
        if not self.link_disconnected_graph:
            return []
        disconnected_run = f"multi-session-path-probe-{self.run_tag}-3"
        if uuid5(NAMESPACE_URL, disconnected_run) not in memory_ids:
            return []
        return self._candidates(
            user_id,
            [f"multi-session-path-probe-{self.run_tag}-2"],
        )[:limit]

    def delete_run(self, run_id: str, *, user_id: str) -> DeletionReport:
        payload = self.runs.get(run_id)
        if payload is None or payload["user_id"] != user_id:
            return DeletionReport(user_id=user_id, run_id=run_id)
        del self.runs[run_id]
        return DeletionReport(
            user_id=user_id,
            run_id=run_id,
            memories_deleted=2,
            sources_deleted=1,
        )


def test_probe_exercises_connected_and_disconnected_paths_then_cleans() -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(run_tag="abc123")
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="abc123",
    )

    assert exit_code == 0
    assert backend.runs == {}
    assert [path for path, _payload in backend.requests] == [
        "/add",
        "/add",
        "/add",
        "/add",
        "/search",
        "/search",
    ]
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {
            "case": "storage",
            "context_count": 4,
            "expected_marker_count": 5,
            "message_count": 4,
            "passed": True,
            "stored_marker_count": 5,
        },
        {
            "case": "relation_isolation",
            "forbidden_marker_count": 0,
            "neighbor_count": 0,
            "passed": True,
        },
        {
            "case": "connected_chain",
            "expected_marker_count": 4,
            "forbidden_marker_count": 0,
            "matched_marker_count": 4,
            "passed": True,
            "returned_count": 3,
        },
        {
            "case": "disconnected_chain",
            "expected_marker_count": 1,
            "forbidden_marker_count": 0,
            "matched_marker_count": 0,
            "passed": True,
            "returned_count": 0,
        },
        {
            "case": "cleanup",
            "cleanup_attempt_error_count": 0,
            "complete": True,
            "first_pass_memories_deleted": 8,
            "first_pass_sources_deleted": 4,
            "passed": True,
            "registered_run_count": 4,
            "residual_memories_deleted": 0,
            "residual_sources_deleted": 0,
            "retry_memories_deleted": 0,
            "retry_sources_deleted": 0,
        },
    ]


def test_probe_failure_is_sanitized_and_registered_runs_are_cleaned() -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(run_tag="failure", fail_on_search=0)
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="failure",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 2
    assert backend.runs == {}
    assert rows[-2] == {
        "case": "probe",
        "failure_category": "request_or_contract",
        "passed": False,
    }
    assert rows[-1]["registered_run_count"] == 4
    assert rows[-1]["passed"] is True
    assert "private-provider-body" not in output.getvalue()


def test_probe_retries_cleanup_and_proves_zero_residuals() -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(run_tag="cleanup")
    output = StringIO()
    failed_once = False

    def transient_delete(run_id: str, *, user_id: str) -> DeletionReport:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("private-cleanup-error")
        return backend.delete_run(run_id, user_id=user_id)

    exit_code = probe.run_probe(
        backend,
        backend,
        transient_delete,
        output,
        run_tag="cleanup",
    )

    cleanup = json.loads(output.getvalue().splitlines()[-1])
    assert exit_code == 0
    assert backend.runs == {}
    assert cleanup == {
        "case": "cleanup",
        "cleanup_attempt_error_count": 1,
        "complete": True,
        "first_pass_memories_deleted": 6,
        "first_pass_sources_deleted": 3,
        "passed": True,
        "registered_run_count": 4,
        "residual_memories_deleted": 0,
        "residual_sources_deleted": 0,
        "retry_memories_deleted": 2,
        "retry_sources_deleted": 1,
    }
    assert "private-cleanup-error" not in output.getvalue()


def test_probe_rejects_a_stored_disconnected_lookalike_in_chain_results() -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(run_tag="leak", leak_disconnected=True)
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="leak",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    connected = next(row for row in rows if row["case"] == "connected_chain")
    assert exit_code == 1
    assert connected["forbidden_marker_count"] == 1
    assert connected["passed"] is False
    assert backend.runs == {}


def test_probe_allows_only_the_disconnected_record_for_its_own_query() -> None:
    """断开查询可返回其否定证据，但不能因此被判成主链泄漏。"""
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(
        run_tag="isolated",
        return_disconnected_evidence=True,
    )
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="isolated",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    disconnected = next(row for row in rows if row["case"] == "disconnected_chain")
    assert exit_code == 0
    assert disconnected == {
        "case": "disconnected_chain",
        "expected_marker_count": 1,
        "forbidden_marker_count": 0,
        "matched_marker_count": 1,
        "passed": True,
        "returned_count": 1,
    }
    assert backend.runs == {}


def test_probe_reports_a_disconnected_relation_to_the_main_chain() -> None:
    """A graph edge from the isolated record must be visible without printing content."""
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(
        run_tag="graph-leak",
        link_disconnected_graph=True,
    )
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="graph-leak",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    relation_isolation = next(
        row for row in rows if row["case"] == "relation_isolation"
    )
    assert exit_code == 1
    assert relation_isolation == {
        "case": "relation_isolation",
        "forbidden_marker_count": 2,
        "neighbor_count": 1,
        "passed": False,
    }
    assert backend.runs == {}
    assert "Root-graph-leak" not in output.getvalue()


def test_probe_output_uses_only_safe_aggregate_fields() -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    backend = _InMemoryBackend(run_tag="safe")
    output = StringIO()

    assert probe.run_probe(
        backend, backend, backend.delete_run, output, run_tag="safe"
    ) == 0

    allowed = {
        "case",
        "context_count",
        "message_count",
        "stored_marker_count",
        "expected_marker_count",
        "matched_marker_count",
        "forbidden_marker_count",
        "neighbor_count",
        "returned_count",
        "passed",
        "cleanup_attempt_error_count",
        "complete",
        "first_pass_memories_deleted",
        "first_pass_sources_deleted",
        "registered_run_count",
        "residual_memories_deleted",
        "residual_sources_deleted",
        "retry_memories_deleted",
        "retry_sources_deleted",
    }
    assert all(set(json.loads(line)).issubset(allowed) for line in output.getvalue().splitlines())
    assert "Root-safe" not in output.getvalue()


def test_main_rejects_non_loopback_url_without_printing_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = importlib.import_module("scripts.multi_session_path_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "official-masm")
    monkeypatch.setenv("DATABASE_URL", "postgresql://private-database")
    monkeypatch.setenv("MASM_API_KEYS", "private-api-key")
    monkeypatch.setenv("MASM_LLM_API_KEY", "private-llm-key")

    exit_code = probe.main(["--base-url", "https://example.com"])

    output = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(output.out) == {
        "case": "preflight",
        "failure_category": "configuration",
        "passed": False,
    }
    assert "private-" not in output.out + output.err
