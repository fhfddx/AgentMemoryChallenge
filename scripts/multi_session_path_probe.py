"""Exercise a deployed multi-session chain with scoped synthetic data."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TextIO
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

from masm.config import RuntimeProfile, Settings
from masm.retrieval.selector_pool import build_selector_pool
from masm.runtime import build_runtime
from masm.schemas.api import SearchRequest
from masm.services.deletion_service import DeletionReport, DeletionService
from masm.services.search_service import SearchService
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
    memory_id: UUID
    request_id: str


class _Repository(Protocol):
    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> Sequence[_StoredCandidate]: ...

    def message_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str], limit: int
    ) -> Sequence[_StoredCandidate]: ...

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> Sequence[_StoredCandidate]: ...


DeleteRun = Callable[..., DeletionReport]
DiagnoseSearch = Callable[
    [str, str, tuple[str, ...], tuple[str, ...]],
    Sequence[dict[str, Any]],
]


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


def _marker_counts(
    candidates: Sequence[Any],
    expected_markers: Sequence[str],
    forbidden_markers: Sequence[str],
) -> tuple[int, int]:
    text = " ".join(_content_text(candidate.content) for candidate in candidates)
    return (
        sum(marker in text for marker in expected_markers),
        sum(marker in text for marker in forbidden_markers),
    )


def _stage_row(
    case: str,
    candidates: Sequence[Any],
    expected_markers: Sequence[str],
    forbidden_markers: Sequence[str],
) -> dict[str, Any]:
    matched, forbidden = _marker_counts(
        candidates,
        expected_markers,
        forbidden_markers,
    )
    return {
        "case": case,
        "candidate_count": len(candidates),
        "matched_marker_count": matched,
        "forbidden_marker_count": forbidden,
    }


def _diagnose_search_stages(
    service: SearchService,
    user_id: str,
    query: str,
    expected_markers: tuple[str, ...],
    forbidden_markers: tuple[str, ...],
    *,
    selector_max_candidates: int,
) -> list[dict[str, Any]]:
    """Return content-free aggregate counts at each deployed Search stage."""
    request = SearchRequest(query=query, user_id=user_id, top_k=10)
    parsed = service._analyze(request)  # noqa: SLF001
    recalled, _channel_counts = service._retriever.retrieve_with_stats(  # noqa: SLF001
        user_id,
        parsed,
        30,
    )
    candidates = [candidate for candidate in recalled if candidate.user_id == user_id]
    if service._relevance_gate is not None:  # noqa: SLF001
        candidates = service._relevance_gate.filter(candidates)  # noqa: SLF001
    strong_anchor_ids = {candidate.memory_id for candidate in candidates}
    rows = [
        _stage_row(
            "stage_recall",
            candidates,
            expected_markers,
            forbidden_markers,
        )
    ]

    if service._expander is not None and candidates:  # noqa: SLF001
        candidates = service._with_expansion(user_id, candidates, 10)  # noqa: SLF001
    if service._expander is not None or service._reranker is not None:  # noqa: SLF001
        candidates = service._deduplicate(candidates)  # noqa: SLF001
    rows.append(
        _stage_row(
            "stage_expanded",
            candidates,
            expected_markers,
            forbidden_markers,
        )
    )

    ranked = service._rank(parsed, candidates)  # noqa: SLF001
    selector_pool = build_selector_pool(
        ranked,
        max_candidates=selector_max_candidates,
    )
    rows.append(
        _stage_row(
            "stage_selector_pool",
            selector_pool,
            expected_markers,
            forbidden_markers,
        )
    )

    if service._selector is None:  # noqa: SLF001
        selected = selector_pool
        candidate_count = len(selector_pool)
        evidence_state = "unknown"
        fallback = False
        abstained = not selected
    else:
        selection = service._selector.select(  # noqa: SLF001
            query,
            None,
            ranked,
            strong_anchor_ids,
        )
        selected = list(selection.evidence)
        candidate_count = selection.candidate_count
        evidence_state = selection.evidence_state
        fallback = selection.fallback
        abstained = selection.abstained
    matched, forbidden = _marker_counts(
        selected,
        expected_markers,
        forbidden_markers,
    )
    rows.append(
        {
            "case": "stage_selected",
            "candidate_count": candidate_count,
            "selected_count": len(selected),
            "matched_marker_count": matched,
            "forbidden_marker_count": forbidden,
            "evidence_state": evidence_state,
            "fallback": fallback,
            "abstained": abstained,
            "passed": (
                not selected
                or (
                    matched == len(expected_markers)
                    and forbidden == 0
                )
            ),
        }
    )
    return rows


def run_probe(
    client: _Client,
    repository: MemoryRepository | _Repository,
    delete_run: DeleteRun,
    output: TextIO,
    *,
    run_tag: str,
    diagnose_search: DiagnoseSearch | None = None,
) -> int:
    """Run connected/disconnected chain cases and clean every registered Add."""
    user_id = f"multi-session-path-probe-{run_tag}"
    root = f"Root-{run_tag}"
    bridge = f"Bridge-{run_tag}"
    leaf = f"Leaf-{run_tag}"
    terminal = f"Terminal-{run_tag}"
    missing = f"Unconnected-{run_tag}"
    run_ids = [f"{user_id}-{index}" for index in range(4)]
    registered: list[str] = []
    all_passed = True
    cleanup_passed = False
    failure_exit_code = 0
    try:
        additions = (
            f"The registered chain {root} continues through {bridge}.",
            f"For the same registered chain, {bridge} continues through {leaf}.",
            f"The terminal record associated with {leaf} is {terminal}.",
            (
                f"The isolated catalog entry {missing} uses registered-chain terminology "
                "but belongs to a separate archive."
            ),
        )
        for index, (run_id, content) in enumerate(
            zip(run_ids, additions, strict=True)
        ):
            # Register before transmission so a failed or ambiguous Add is still cleaned.
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
        messages = repository.message_candidates_for_requests(user_id, run_ids, 256)
        stored_text = " ".join(
            candidate.content for candidate in [*contexts, *messages]
        )
        storage_markers = (root, bridge, leaf, terminal, missing)
        stored_marker_count = sum(marker in stored_text for marker in storage_markers)
        storage_passed = (
            len(contexts) == 4
            and len(messages) == 4
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

        context_by_run = {candidate.request_id: candidate for candidate in contexts}
        isolated_context = context_by_run.get(run_ids[-1])
        if isolated_context is None:
            raise RuntimeError("Stored context contract")
        relation_neighbors = repository.related(
            user_id,
            [isolated_context.memory_id],
            32,
        )
        relation_text = " ".join(candidate.content for candidate in relation_neighbors)
        relation_forbidden_count = sum(
            marker in relation_text for marker in (root, bridge, leaf, terminal)
        )
        relation_isolation_passed = relation_forbidden_count == 0
        all_passed &= relation_isolation_passed
        _print_row(
            output,
            {
                "case": "relation_isolation",
                "neighbor_count": len(relation_neighbors),
                "forbidden_marker_count": relation_forbidden_count,
                "passed": relation_isolation_passed,
            },
        )

        disconnected_query = f"What terminal record is linked to {missing}?"
        disconnected_expected = (missing,)
        disconnected_forbidden = (root, bridge, leaf, terminal)
        if diagnose_search is not None:
            for row in diagnose_search(
                user_id,
                disconnected_query,
                disconnected_expected,
                disconnected_forbidden,
            ):
                _print_row(output, row)
                if row.get("case") == "stage_selected":
                    all_passed &= row.get("passed") is True

        searches = (
            (
                "connected_chain",
                f"Follow the registered chain from {root} and identify its terminal record.",
                (root, bridge, leaf, terminal),
                (missing,),
                False,
            ),
            (
                "disconnected_chain",
                disconnected_query,
                disconnected_expected,
                disconnected_forbidden,
                True,
            ),
        )
        for name, query, expected_markers, forbidden_markers, allow_empty in searches:
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
            forbidden_marker_count = sum(
                marker in returned_text for marker in forbidden_markers
            )
            passed = (
                (allow_empty and len(data) == 0)
                or (
                    matched_marker_count == len(expected_markers)
                    and forbidden_marker_count == 0
                )
            )
            all_passed &= passed
            _print_row(
                output,
                {
                    "case": name,
                    "returned_count": len(data),
                    "matched_marker_count": matched_marker_count,
                    "forbidden_marker_count": forbidden_marker_count,
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
    parser = argparse.ArgumentParser(description="Run synthetic multi-session diagnostics")
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
    runtime = None
    try:
        database = Database.create(settings.database_url)
        repository = MemoryRepository(database)
        deletion = DeletionService(repository, AssetStore(settings.asset_dir))
        runtime = build_runtime(settings, repository)
        stage_service = SearchService(
            runtime.retriever,
            max_image_bytes=settings.max_image_bytes,
            analyzer=runtime.query_analyzer,
            expander=runtime.relation_expander,
            reranker=runtime.reranker,
            relevance_gate=runtime.relevance_gate,
            selector=runtime.evidence_selector,
            runtime_profile=settings.runtime_profile.value,
        )

        def diagnose_search(
            user_id: str,
            query: str,
            expected_markers: tuple[str, ...],
            forbidden_markers: tuple[str, ...],
        ) -> Sequence[dict[str, Any]]:
            return _diagnose_search_stages(
                stage_service,
                user_id,
                query,
                expected_markers,
                forbidden_markers,
                selector_max_candidates=settings.selector_max_candidates,
            )

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
                diagnose_search=diagnose_search,
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
        if runtime is not None:
            runtime.close()
        if database is not None:
            database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
