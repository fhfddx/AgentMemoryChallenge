"""The large-store probe measures refusals under noise and cleans every registered Add."""

import importlib
import json
from dataclasses import dataclass
from io import StringIO
from typing import Any

from masm.services.deletion_service import DeletionReport

probe = importlib.import_module("scripts.abstention_noise_probe")


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.payload


class _Backend:
    """Two positives answered; every refusal shape refused unless `leak` names one."""

    def __init__(self, run_tag: str, leak: str | None = None) -> None:
        self.run_tag = run_tag
        self.leak = leak
        self.runs: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _Response:
        payload = json_body if json_body is not None else kwargs["json"]
        if path == "/add":
            self.runs[payload["request_id"]] = payload
            return _Response(200, {"success": True, "request_id": payload["request_id"]})
        assert path == "/search"
        query = payload["query"]
        self.queries.append(query)
        item = f"Item-{self.run_tag}"
        if item in query and "access code" in query:
            content = f"Code is Code-{self.run_tag}."
            return _Response(200, {"data": [{"id": "a", "content": content}]})
        if item in query and query.startswith("Where"):
            content = "It is on the third floor of the Harbour building."
            return _Response(200, {"data": [{"id": "b", "content": content}]})
        if self.leak is not None and self.leak in query:
            return _Response(200, {"data": [{"id": "leak", "content": "Unrelated note."}]})
        return _Response(200, {"data": []})


class _Repository:
    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def context_candidates_for_requests(
        self, user_id: str, request_ids: list[str]
    ) -> list[Any]:
        del user_id
        return [
            type("Candidate", (), {"content": payload["messages"][0]["content"]})()
            for run_id, payload in self._backend.runs.items()
            if run_id in request_ids
        ]

    def message_candidates_for_requests(
        self, user_id: str, request_ids: list[str], limit: int
    ) -> list[Any]:
        del user_id, request_ids, limit
        return []


_DELETED: set[str] = set()


def _delete_run(run_id: str, *, user_id: str) -> DeletionReport:
    if run_id in _DELETED:
        return DeletionReport(user_id=user_id, run_id=run_id)
    _DELETED.add(run_id)
    return DeletionReport(user_id=user_id, run_id=run_id, memories_deleted=1, sources_deleted=1)


def _run(backend: _Backend, topics: int = 3) -> tuple[int, list[dict[str, Any]]]:
    output = StringIO()
    code = probe.run_probe(
        backend,  # type: ignore[arg-type]
        _Repository(backend),  # type: ignore[arg-type]
        _delete_run,
        output,
        run_tag=backend.run_tag,
        topics=topics,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return code, rows


def test_probe_passes_when_the_noisy_store_still_refuses() -> None:
    code, rows = _run(_Backend("n1"))

    by_case = {row["case"]: row for row in rows}
    assert code == 0
    assert by_case["positive_code"]["passed"] is True
    assert by_case["positive_location"]["passed"] is True
    for name in (
        "absent_subject",
        "absent_attribute",
        "absent_quantity",
        "relation_absent",
        "option_overlap",
        "absent_temporal",
        "trap_other_door",
        "trap_adjacent_colour",
        "trap_other_sibling",
        "trap_partial_date",
        "trap_attribute_swap",
        "trap_other_painting",
        "trap_other_invoice",
        "trap_known_subject_missing_attribute",
    ):
        assert by_case[name]["refusal_expected"] is True
        assert by_case[name]["returned_count"] == 0
        assert by_case[name]["passed"] is True


def test_probe_fails_when_noise_leaks_into_a_refusal() -> None:
    code, rows = _run(_Backend("n2", leak="colour"))

    by_case = {row["case"]: row for row in rows}
    assert code == 1
    assert by_case["absent_attribute"]["returned_count"] == 1
    assert by_case["absent_attribute"]["passed"] is False
    assert by_case["absent_subject"]["passed"] is True


def test_probe_seeds_and_cleans_every_registered_add() -> None:
    backend = _Backend("n3")
    code, rows = _run(backend, topics=4)

    cleanup = next(row for row in rows if row["case"] == "cleanup")
    assert code == 0
    # 十一条固定记忆（目标两条 + 近失配素材九条）+ 每主题两条噪声记忆。
    assert cleanup["registered_run_count"] == 11 + 2 * 4
    assert cleanup["registered_run_count"] == len(backend.runs)
    assert cleanup["residual_memories_deleted"] == 0


def test_probe_rows_carry_counts_not_memory_text() -> None:
    _, rows = _run(_Backend("n4"))

    rendered = json.dumps(rows, sort_keys=True)
    for marker in ("Code-n4", "Item-n4", "Harbour building", "shared folder"):
        assert marker not in rendered
