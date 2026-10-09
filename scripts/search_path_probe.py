"""Exercise the deployed Add/Search path with scoped synthetic data."""

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
        raise RuntimeError(f"{path} HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} response shape")
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
    """Run three synthetic retrieval boundaries and clean every registered Add."""
    user_id = f"search-path-probe-{run_tag}"
    project = f"Atlas-{run_tag}"
    code_name = f"Cobalt-{run_tag}"
    cabinet = f"Cabinet-Seven-{run_tag}"
    review_date = "November 14, 2026"
    run_ids = [f"{user_id}-0", f"{user_id}-1"]
    registered: list[str] = []
    all_passed = True
    cleanup_passed = False
    failure_exit_code = 0
    try:
        additions = (
            (
                f"The {project} migration uses code name {code_name}, and its dossier "
                f"is stored in {cabinet}.",
                "project-facts",
            ),
            (
                f"The review for that migration is scheduled on {review_date}.",
                "project-review",
            ),
        )
        for run_id, (content, session_suffix) in zip(run_ids, additions, strict=True):
            registered.append(run_id)
            body = _post(
                client,
                "/add",
                {
                    "request_id": run_id,
                    "user_id": user_id,
                    "session_id": f"{user_id}-{session_suffix}",
                    "messages": [{"role": "user", "content": content}],
                },
            )
            if body.get("success") is not True:
                raise RuntimeError("Add contract")

        contexts = repository.context_candidates_for_requests(user_id, run_ids)
        messages = repository.message_candidates_for_requests(user_id, run_ids, 256)
        stored_text = " ".join(
            candidate.content for candidate in [*contexts, *messages]
        )
        storage_markers = (project, code_name, cabinet, review_date)
        stored_marker_count = sum(marker in stored_text for marker in storage_markers)
        storage_passed = (
            len(contexts) == 2 and len(messages) == 2
            and stored_marker_count == len(storage_markers)
        )
        all_passed &= storage_passed
        _print_row(
            output,
            {
                "case": "storage",
                "context_count": len(contexts),
                "message_count": len(messages),
                "stored_marker_count": stored_marker_count,
                "expected_marker_count": len(storage_markers),
                "passed": storage_passed,
            },
        )

        searches = (
            (
                "direct_recall",
                f"Where is the {project} migration dossier kept?",
                (cabinet,),
            ),
            (
                "multi_session",
                f"What is the {project} migration code name, and when is its review?",
                (code_name, review_date),
            ),
            (
                "same_entity_abstention",
                f"What is the {project} migration budget authorization code?",
                (),
            ),
        )
        for name, query, expected_markers in searches:
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
            passed = (
                len(data) == 0
                if not expected_markers
                else matched_marker_count == len(expected_markers)
            )
            all_passed &= passed
            _print_row(
                output,
                {
                    "case": name,
                    "returned_count": len(data),
                    "matched_marker_count": matched_marker_count,
                    "expected_marker_count": len(expected_markers),
                    "passed": passed,
                },
            )
    except RuntimeError:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {
                "case": "probe",
                "failure_category": "request_or_contract",
                "passed": False,
            },
        )
    except Exception:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {
                "case": "probe",
                "failure_category": "internal_error",
                "passed": False,
            },
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
                "first_pass_memories_deleted": sum(
                    report.memories_deleted for report in first
                ),
                "first_pass_sources_deleted": sum(report.sources_deleted for report in first),
                "retry_memories_deleted": sum(
                    report.memories_deleted for report in retry
                ),
                "retry_sources_deleted": sum(report.sources_deleted for report in retry),
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
        {
            "case": "preflight",
            "failure_category": "configuration",
            "passed": False,
        },
    )


def main(argv: list[str] | None = None) -> int:
    """Run inside the deployed API container against its loopback endpoint."""
    parser = argparse.ArgumentParser(description="Run synthetic Add/Search path diagnostics")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args([] if argv is None else argv)
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.selector_enabled
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
            {
                "case": "setup",
                "failure_category": "internal_error",
                "passed": False,
            },
        )
        return 2
    finally:
        if database is not None:
            database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
