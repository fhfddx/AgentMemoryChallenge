"""Exercise the deployed multi-session *bridging* behaviour with scoped synthetic data.

The existing multi-session probe asks a question that names the chain's own entry point, so the
retrieval layer can follow the chain from a named anchor and the probe passes. Real multi-session
questions are harder: the question names neither the bridging entity nor the answer's own source,
so the system has to join two sessions through evidence inside the store.

This probe seeds exactly that shape and asks the question without naming the bridge:

- session A states who the sister works for;
- session B states the date of that company's party;
- the question asks for the date, naming only "my sister".

Both sessions must appear in the returned evidence: the bridge is the only way to connect the
question to the date. A control query that does name the bridge checks the join is not simply
broken everywhere. All Add requests are registered before transmission and cleaned up in three
rounds.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TextIO
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from masm.config import RuntimeProfile, Settings
from masm.services.deletion_service import DeletionReport, DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _Client(Protocol):
    def post(self, path: str, *, json: dict[str, Any]) -> _Response: ...


class _StoredCandidate(Protocol):
    content: str


class _Repository(Protocol):
    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> Sequence[_StoredCandidate]: ...

    def message_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str], limit: int
    ) -> Sequence[_StoredCandidate]: ...


DeleteRun = Callable[..., DeletionReport]


def _post(client: _Client, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"{path} status")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} body")
    return body


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)


def _print_row(output: TextIO, row: dict[str, Any]) -> None:
    print(json.dumps(row, sort_keys=True), file=output)


def run_probe(
    client: _Client,
    repository: MemoryRepository | _Repository,
    delete_run: DeleteRun,
    output: TextIO,
    *,
    run_tag: str,
) -> int:
    """Seed a bridged pair of sessions, ask without naming the bridge, then clean up."""
    user_id = f"multi-session-bridge-probe-{run_tag}"
    sister = f"Sister-{run_tag}"
    office = f"Office-{run_tag}"
    date = f"Date-{run_tag}"
    distractor = f"Unrelated-{run_tag}"

    additions = (
        f"My sister {sister} works at the {office} office as an architect.",
        f"The {office} holiday party is scheduled for {date}.",
        f"The {distractor} archive keeps unrelated shipping records.",
    )
    run_ids = [f"{user_id}-{index}" for index in range(len(additions))]
    registered: list[str] = []
    all_passed = True
    cleanup_passed = False
    failure_exit_code = 0
    try:
        for index, (run_id, content) in enumerate(zip(run_ids, additions, strict=True)):
            registered.append(run_id)
            body = _post(
                client,
                "/add",
                {
                    "request_id": run_id,
                    "user_id": user_id,
                    "session_id": f"{user_id}-session-{index}",
                    "messages": [{"role": "user", "content": content}],
                },
            )
            if body.get("success") is not True:
                raise RuntimeError("Add contract")

        contexts = repository.context_candidates_for_requests(user_id, run_ids)
        stored_text = " ".join(candidate.content for candidate in contexts)
        storage_markers = (sister, office, date)
        stored_marker_count = sum(marker in stored_text for marker in storage_markers)
        storage_passed = bool(stored_text) and stored_marker_count == len(storage_markers)
        all_passed &= storage_passed
        _print_row(
            output,
            {
                "case": "storage",
                "context_count": len(contexts),
                "stored_marker_count": stored_marker_count,
                "expected_marker_count": len(storage_markers),
                "passed": storage_passed,
            },
        )

        # 桥接问题不点名 office：必须靠库里"A 说她姐姐在 office"才能找到 B 的日期。
        cases = (
            ("bridged_question", "When is my sister's company holiday party?", (office, date)),
            ("named_control", f"When is the {office} holiday party?", (date,)),
            ("unrelated_question", "When is the shipping archive audit?", ()),
        )
        for name, query, expected_markers in cases:
            body = _post(
                client,
                "/search",
                {"query": query, "user_id": user_id, "top_k": 10},
            )
            data = body.get("data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise RuntimeError("Search contract")
            returned_text = " ".join(_content_text(row.get("content")) for row in data)
            matched_marker_count = sum(
                marker in returned_text for marker in expected_markers
            )
            forbidden_marker_count = int(distractor in returned_text)
            if expected_markers:
                passed = (
                    matched_marker_count == len(expected_markers)
                    and forbidden_marker_count == 0
                )
            else:
                passed = len(data) == 0
            all_passed &= passed
            _print_row(
                output,
                {
                    "case": name,
                    "returned_count": len(data),
                    "matched_marker_count": matched_marker_count,
                    "expected_marker_count": len(expected_markers),
                    "forbidden_marker_count": forbidden_marker_count,
                    "passed": passed,
                },
            )
    except RuntimeError:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {"case": "probe", "failure_category": "request_or_contract", "passed": False},
        )
    except Exception:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {"case": "probe", "failure_category": "internal_error", "passed": False},
        )
    finally:

        def cleanup_round() -> tuple[list[DeletionReport], int]:
            reports: list[DeletionReport] = []
            error_count = 0
            for run_id in registered:
                try:
                    reports.append(delete_run(run_id, user_id=user_id))
                except Exception:
                    error_count += 1
            return reports, error_count

        first, first_errors = cleanup_round()
        retry, retry_errors = cleanup_round()
        residual, residual_errors = cleanup_round()
        residual_memories = sum(report.memories_deleted for report in residual)
        residual_sources = sum(report.sources_deleted for report in residual)
        cleanup_passed = (
            residual_errors == 0
            and len(residual) == len(registered)
            and all(report.complete for report in residual)
            and residual_memories == 0
            and residual_sources == 0
        )
        _print_row(
            output,
            {
                "case": "cleanup",
                "registered_run_count": len(registered),
                "cleanup_attempt_error_count": (
                    first_errors + retry_errors + residual_errors
                ),
                "first_pass_memories_deleted": sum(r.memories_deleted for r in first),
                "first_pass_sources_deleted": sum(r.sources_deleted for r in first),
                "retry_memories_deleted": sum(r.memories_deleted for r in retry),
                "retry_sources_deleted": sum(r.sources_deleted for r in retry),
                "residual_memories_deleted": residual_memories,
                "residual_sources_deleted": residual_sources,
                "complete": cleanup_passed,
                "passed": cleanup_passed,
            },
        )
    if failure_exit_code:
        return failure_exit_code
    return 0 if all_passed and cleanup_passed else 1


def _local_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("probe base URL must be local")
    return value.rstrip("/")


def _report_preflight_failure() -> None:
    _print_row(
        sys.stdout,
        {"case": "preflight", "failure_category": "configuration", "passed": False},
    )


def main(argv: list[str] | None = None) -> int:
    """Run inside the deployed API container against its loopback endpoint."""
    parser = argparse.ArgumentParser(description="Run synthetic bridged multi-session diagnostics")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args([] if argv is None else argv)
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.database_url
            or not settings.api_keys
            or args.timeout <= 0
        ):
            raise ValueError("incomplete probe configuration")
        base_url = _local_base_url(args.base_url)
    except Exception:
        _report_preflight_failure()
        return 2

    database: Database | None = None
    try:
        database = Database.create(settings.database_url)
        repository = MemoryRepository(database)
        deletion = DeletionService(repository, AssetStore(settings.asset_dir))
        with httpx.Client(
            base_url=base_url,
            headers={"X-Api-Key": settings.api_keys[0]},
            timeout=args.timeout,
        ) as client:
            return run_probe(
                client,
                repository,
                deletion.delete_run,
                sys.stdout,
                run_tag=uuid4().hex[:12],
            )
    except Exception:
        _print_row(
            sys.stdout,
            {"case": "setup", "failure_category": "internal_error", "passed": False},
        )
        return 2
    finally:
        if database is not None:
            database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
