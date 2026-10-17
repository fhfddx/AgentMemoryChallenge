"""The abstention probe measures refusal cases and cleans every registered Add."""

import importlib
import json
from dataclasses import dataclass
from io import StringIO
from typing import Any

from masm.services.deletion_service import DeletionReport

probe = importlib.import_module("scripts.abstention_path_probe")


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.payload


class _Backend:
    """回答正例、默认拒答负例；`leak` 指定哪个负例错误地返回证据。"""

    def __init__(self, run_tag: str, leak: str | None = None) -> None:
        self.run_tag = run_tag
        self.leak = leak
        self.runs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _Response:
        payload = json_body if json_body is not None else kwargs["json"]
        self.requests.append((path, payload))
        if path == "/add":
            self.runs[payload["request_id"]] = payload
            return _Response(200, {"success": True, "request_id": payload["request_id"]})
        assert path == "/search"
        query = payload["query"]
        self.queries.append(query)
        item = f"Item-{self.run_tag}"
        code = f"Code-{self.run_tag}"
        office = f"Office-{self.run_tag}"
        if item in query and "access code" in query:
            return _Response(200, {"data": [{"id": "a", "content": f"The code is {code}."}]})
        if "sister" in query:
            return _Response(200, {"data": [{"id": "b", "content": f"She works at the {office}."}]})
        if self.leak is not None and self.leak in query:
            return _Response(
                200,
                {"data": [{"id": "leak", "content": f"Unrelated mention of {item}."}]},
            )
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
        del user_id, limit
        return []


_DELETED: set[str] = set()


def _delete_run(run_id: str, *, user_id: str) -> DeletionReport:
    # 真实删除是幂等的：第一轮计入删除数，之后为 0；探针据此判断残留。
    if run_id in _DELETED:
        return DeletionReport(user_id=user_id, run_id=run_id)
    _DELETED.add(run_id)
    return DeletionReport(
        user_id=user_id,
        run_id=run_id,
        memories_deleted=1,
        sources_deleted=1,
    )


def _run(backend: _Backend) -> tuple[int, list[dict[str, Any]]]:
    output = StringIO()
    code = probe.run_probe(
        backend,  # type: ignore[arg-type]
        _Repository(backend),  # type: ignore[arg-type]
        _delete_run,
        output,
        run_tag=backend.run_tag,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return code, rows


def test_probe_passes_when_the_backend_refuses_the_negative_cases() -> None:
    code, rows = _run(_Backend("t1"))

    by_case = {row["case"]: row for row in rows}
    assert code == 0
    assert by_case["storage"]["passed"] is True
    assert by_case["positive_direct"]["passed"] is True
    assert by_case["positive_relation"]["passed"] is True
    for name in (
        "absent_subject",
        "absent_attribute",
        "wrong_subject",
        "option_overlap",
    ):
        assert by_case[name]["refusal_expected"] is True
        assert by_case[name]["returned_count"] == 0
        assert by_case[name]["passed"] is True
    assert by_case["cleanup"]["complete"] is True


def test_probe_fails_when_a_negative_case_leaks_evidence() -> None:
    code, rows = _run(_Backend("t2", leak="colour"))

    by_case = {row["case"]: row for row in rows}
    assert code == 1
    assert by_case["absent_attribute"]["returned_count"] == 1
    assert by_case["absent_attribute"]["passed"] is False
    assert by_case["absent_subject"]["passed"] is True


def test_probe_cleans_every_registered_add() -> None:
    backend = _Backend("t3")
    code, rows = _run(backend)

    cleanup = next(row for row in rows if row["case"] == "cleanup")
    assert code == 0
    assert cleanup["registered_run_count"] == len(backend.runs)
    assert cleanup["residual_memories_deleted"] == 0
    assert cleanup["residual_sources_deleted"] == 0


def test_probe_rows_carry_counts_not_memory_text() -> None:
    _, rows = _run(_Backend("t4"))

    rendered = json.dumps(rows, sort_keys=True)
    for marker in ("Code-t4", "Item-t4", "Office-t4", "Sister-t4"):
        assert marker not in rendered
