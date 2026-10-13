"""Probe additive semantic expansion with a crowded cross-request chain."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TextIO, cast
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from masm.config import RuntimeProfile, Settings
from masm.retrieval.evidence_selector import SelectionResult
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.selector_pool import build_selector_pool
from masm.runtime import RuntimeComponents, build_runtime
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


class _Repository(Protocol):
    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> Sequence[_StoredCandidate]: ...

    def message_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str], limit: int
    ) -> Sequence[_StoredCandidate]: ...


DeleteRun = Callable[..., DeletionReport]
PoolDiagnostic = Callable[[str, str, dict[str, str]], dict[str, Any]]

_SAFE_SIGNAL_NAMES = frozenset({"lexical", "text_vector", "image_vector", "metadata"})
_MISSING_PHRASES = (
    "not been documented",
    "not documented",
    "unknown",
    "unavailable",
    "cannot be determined",
)


class _PoolCaptureSelector:
    """Capture only non-content selector-pool features for deployment diagnosis."""

    def __init__(self, markers: dict[str, str], *, max_candidates: int) -> None:
        self._markers = markers
        self._max_candidates = max_candidates
        self.snapshot: dict[str, Any] = {
            "candidate_count": 0,
            "source_count": 0,
            "candidates": [],
        }

    def select(
        self,
        query: str | Sequence[Any],
        options: Sequence[str] | None,
        ranked: Sequence[RankedEvidence],
        strong_anchor_ids: set[Any],
    ) -> SelectionResult:
        del query, options
        pool = build_selector_pool(ranked, max_candidates=self._max_candidates)
        source_ordinals: dict[str, int] = {}
        features: list[dict[str, Any]] = []
        for item in pool:
            source_key = item.request_id or str(item.memory_id)
            source_ordinals.setdefault(source_key, len(source_ordinals) + 1)
            text = (
                item.content
                if isinstance(item.content, str)
                else " ".join(
                    str(getattr(part, "text", "")) for part in item.content
                )
            )
            lowered = text.casefold()
            features.append(
                {
                    "source_ordinal": source_ordinals[source_key],
                    "rank": item.rank,
                    "granularity": item.granularity,
                    "is_strong_anchor": item.memory_id in strong_anchor_ids,
                    "contains_question_subject": self._markers[
                        "question_subject"
                    ].casefold()
                    in lowered,
                    "contains_other_subject": self._markers["other_subject"].casefold()
                    in lowered,
                    "contains_relay": self._markers["relay"].casefold() in lowered,
                    "contains_terminal": self._markers["terminal"].casefold() in lowered,
                    "contains_missing_statement": any(
                        phrase in lowered for phrase in _MISSING_PHRASES
                    ),
                    "signal_names": sorted(
                        name for name in item.metadata if name in _SAFE_SIGNAL_NAMES
                    ),
                }
            )
        self.snapshot = {
            "candidate_count": len(pool),
            "source_count": len(source_ordinals),
            "candidates": features,
        }
        return SelectionResult(
            (),
            len(pool),
            len(source_ordinals),
            0,
            False,
            True,
            "none",
            "insufficient",
        )


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
    pool_diagnostic: PoolDiagnostic | None = None,
) -> int:
    """Exercise a crowded semantic chain and an isolated abstention boundary."""
    user_id = f"semantic-expansion-probe-{run_tag}"
    dossier = f"Dossier-{run_tag}"
    relay = f"Relay-{run_tag}"
    terminal = f"Compartment-{run_tag}"
    isolated = f"Isolated-{run_tag}"
    run_ids = [f"{user_id}-{index}" for index in range(8)]
    additions = [
        (
            f"Records for {dossier} are assigned to the transfer phrase {relay}. "
            "That phrase is the only routing link for this dossier."
        ),
        (
            f"Every record routed by the transfer phrase {relay} ultimately arrives "
            f"at {terminal}."
        ),
        *(
            f"Administrative note {index} for {dossier} discusses record filing and "
            "archive routing, but it records neither a transfer phrase nor a destination."
            for index in range(1, 6)
        ),
        (
            f"{isolated} is an independent dossier registry entry. Its record routing "
            "destination has not been documented."
        ),
    ]
    registered: list[str] = []
    all_passed = True
    cleanup_passed = False
    failure_exit_code = 0
    try:
        for index, (run_id, content) in enumerate(
            zip(run_ids, additions, strict=True)
        ):
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
        storage_markers = (dossier, relay, terminal, isolated)
        stored_marker_count = sum(marker in stored_text for marker in storage_markers)
        storage_passed = (
            len(contexts) == len(run_ids)
            and len(messages) == len(run_ids)
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

        isolated_query = (
            f"Which compartment do records for {isolated} ultimately reach?"
        )
        if pool_diagnostic is not None:
            snapshot = pool_diagnostic(
                user_id,
                isolated_query,
                {
                    "question_subject": isolated,
                    "other_subject": dossier,
                    "relay": relay,
                    "terminal": terminal,
                },
            )
            _print_row(output, {"case": "selector_pool", **snapshot})

        searches = (
            (
                "connected_semantic_chain",
                f"Which compartment do the records for {dossier} ultimately reach?",
                (dossier, relay, terminal),
                (isolated,),
            ),
            (
                "isolated_abstention",
                isolated_query,
                (),
                (terminal,),
            ),
        )
        for name, query, expected_markers, forbidden_markers in searches:
            body = _post(
                client,
                "/search",
                {"query": query, "user_id": user_id, "top_k": 3},
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
                len(data) == 0 and forbidden_marker_count == 0
                if not expected_markers
                else matched_marker_count == len(expected_markers)
                and forbidden_marker_count == 0
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


def main(argv: list[str] | None = None) -> int:
    """Run in a helper container against the deployed API loopback endpoint."""
    parser = argparse.ArgumentParser(description="Run semantic expansion diagnostics")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--pool-diagnostic", action="store_true")
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
        _print_row(
            sys.stdout,
            {
                "case": "preflight",
                "failure_category": "configuration",
                "passed": False,
            },
        )
        return 2

    database: Database | None = None
    runtime: RuntimeComponents | None = None
    try:
        database = Database.create(settings.database_url)
        repository = MemoryRepository(database)
        deletion = DeletionService(repository, AssetStore(settings.asset_dir))
        pool_diagnostic: PoolDiagnostic | None = None
        if args.pool_diagnostic:
            runtime = build_runtime(settings, repository)

            def capture_pool(
                user_id: str, query: str, markers: dict[str, str]
            ) -> dict[str, Any]:
                selector = _PoolCaptureSelector(
                    markers,
                    max_candidates=settings.selector_max_candidates,
                )
                service = SearchService(
                    runtime.retriever,
                    max_image_bytes=settings.max_image_bytes,
                    analyzer=runtime.query_analyzer,
                    expander=runtime.relation_expander,
                    semantic_expander=runtime.semantic_expander,
                    reranker=runtime.reranker,
                    relevance_gate=runtime.relevance_gate,
                    selector=cast(Any, selector),
                    runtime_profile=settings.runtime_profile.value,
                )
                service.search(SearchRequest(query=query, user_id=user_id, top_k=3))
                return selector.snapshot

            pool_diagnostic = capture_pool
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
                pool_diagnostic=pool_diagnostic,
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
