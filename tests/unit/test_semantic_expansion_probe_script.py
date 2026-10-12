"""Semantic-expansion deployment probe contract and output-safety tests."""

import importlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from io import StringIO
from types import SimpleNamespace
from typing import Any

from masm.services.deletion_service import DeletionReport


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.payload


class _Backend:
    def __init__(self, *, run_tag: str, leak_isolated: bool = False) -> None:
        self.run_tag = run_tag
        self.leak_isolated = leak_isolated
        self.runs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.search_index = 0

    def post(self, path: str, *, json: dict[str, Any]) -> _Response:
        self.requests.append((path, json))
        if path == "/add":
            self.runs[json["request_id"]] = json
            return _Response(200, {"success": True})

        dossier = f"Dossier-{self.run_tag}"
        relay = f"Relay-{self.run_tag}"
        terminal = f"Compartment-{self.run_tag}"
        isolated = f"Isolated-{self.run_tag}"
        if self.search_index == 0:
            payload = {
                "data": [
                    {"id": "seed", "content": f"{dossier} follows {relay}."},
                    {"id": "target", "content": f"{relay} reaches {terminal}."},
                ]
            }
        else:
            payload = (
                {"data": [{"id": "leak", "content": f"{isolated} reaches {terminal}."}]}
                if self.leak_isolated
                else {"data": []}
            )
        self.search_index += 1
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
            SimpleNamespace(content=self.runs[run_id]["messages"][0]["content"])
            for run_id in request_ids
            if run_id in self.runs and self.runs[run_id]["user_id"] == user_id
        ]

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


def test_probe_requires_connected_recovery_and_cleans_every_run() -> None:
    probe = importlib.import_module("scripts.semantic_expansion_probe")
    backend = _Backend(run_tag="abc123")
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="abc123",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 0
    assert backend.runs == {}
    assert [path for path, _payload in backend.requests] == [
        *("/add" for _ in range(8)),
        "/search",
        "/search",
    ]
    assert rows == [
        {
            "case": "storage",
            "context_count": 8,
            "expected_marker_count": 4,
            "message_count": 8,
            "passed": True,
            "stored_marker_count": 4,
        },
        {
            "case": "connected_semantic_chain",
            "expected_marker_count": 3,
            "forbidden_marker_count": 0,
            "matched_marker_count": 3,
            "passed": True,
            "returned_count": 2,
        },
        {
            "case": "isolated_abstention",
            "expected_marker_count": 0,
            "forbidden_marker_count": 0,
            "matched_marker_count": 0,
            "passed": True,
            "returned_count": 0,
        },
        {
            "case": "cleanup",
            "cleanup_attempt_error_count": 0,
            "complete": True,
            "first_pass_memories_deleted": 16,
            "first_pass_sources_deleted": 8,
            "passed": True,
            "registered_run_count": 8,
            "residual_memories_deleted": 0,
            "residual_sources_deleted": 0,
            "retry_memories_deleted": 0,
            "retry_sources_deleted": 0,
        },
    ]


def test_probe_rejects_isolated_leak_and_prints_only_safe_aggregates() -> None:
    probe = importlib.import_module("scripts.semantic_expansion_probe")
    backend = _Backend(run_tag="private-marker", leak_isolated=True)
    output = StringIO()

    exit_code = probe.run_probe(
        backend,
        backend,
        backend.delete_run,
        output,
        run_tag="private-marker",
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    isolated = next(row for row in rows if row["case"] == "isolated_abstention")
    allowed = {
        "case",
        "context_count",
        "message_count",
        "stored_marker_count",
        "expected_marker_count",
        "matched_marker_count",
        "forbidden_marker_count",
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
    assert exit_code == 1
    assert isolated["forbidden_marker_count"] == 1
    assert isolated["passed"] is False
    assert backend.runs == {}
    assert all(set(row).issubset(allowed) for row in rows)
    assert "private-marker" not in output.getvalue()
