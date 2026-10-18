"""The bridge probe asks a question that never names the bridging entity."""

import importlib
import json
from dataclasses import dataclass
from io import StringIO
from typing import Any

from masm.services.deletion_service import DeletionReport

probe = importlib.import_module("scripts.multi_session_bridge_probe")


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.payload


class _Backend:
    """默认让桥接问题拿到两段证据；`drop_bridge` 时只返回日期所在的那一段。"""

    def __init__(self, run_tag: str, *, drop_bridge: bool = False) -> None:
        self.run_tag = run_tag
        self.drop_bridge = drop_bridge
        self.runs: dict[str, dict[str, Any]] = {}

    def post(self, path: str, *, json: dict[str, Any] | None = None, **kwargs: Any) -> _Response:
        payload = json if json is not None else kwargs["json_body"]
        if path == "/add":
            self.runs[payload["request_id"]] = payload
            return _Response(200, {"success": True, "request_id": payload["request_id"]})
        assert path == "/search"
        query = payload["query"]
        office = f"Office-{self.run_tag}"
        date = f"Date-{self.run_tag}"
        sister = f"Sister-{self.run_tag}"
        bridges = [
            {"id": "a", "content": f"My sister {sister} works at the {office} office."},
            {"id": "b", "content": f"The {office} holiday party is on {date}."},
        ]
        if "sister" in query:
            if self.drop_bridge:
                # 桥接失败时只拿到日期，另一段（含 office）没有出现。
                return _Response(200, {"data": [{"id": "b", "content": f"Party on {date}."}]})
            return _Response(200, {"data": bridges})
        if office in query:
            return _Response(200, {"data": [{"id": "b", "content": f"Party on {date}."}]})
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
    return DeletionReport(
        user_id=user_id, run_id=run_id, memories_deleted=1, sources_deleted=1
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


def test_bridged_question_passes_when_both_sessions_are_returned() -> None:
    code, rows = _run(_Backend("b1"))

    by_case = {row["case"]: row for row in rows}
    assert code == 0
    assert by_case["bridged_question"]["matched_marker_count"] == 2
    assert by_case["bridged_question"]["passed"] is True
    assert by_case["named_control"]["passed"] is True
    assert by_case["cleanup"]["passed"] is True


def test_bridged_question_fails_when_the_bridge_session_is_missing() -> None:
    code, rows = _run(_Backend("b2", drop_bridge=True))

    by_case = {row["case"]: row for row in rows}
    assert code == 1
    # 只剩日期所在会话：office 标记缺失 → 桥接未建立。
    assert by_case["bridged_question"]["matched_marker_count"] == 1
    assert by_case["bridged_question"]["passed"] is False
    assert by_case["named_control"]["passed"] is True


def test_rows_never_carry_memory_text() -> None:
    _, rows = _run(_Backend("b3"))

    rendered = json.dumps(rows, sort_keys=True)
    for marker in ("Sister-b3", "Office-b3", "Date-b3", "Unrelated-b3"):
        assert marker not in rendered
