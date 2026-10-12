"""Diagnose Add-time relation-link stages without emitting content or identifiers."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TextIO, cast
from uuid import UUID, uuid4

from masm.api.app import create_app
from masm.config import RuntimeProfile, Settings
from masm.orchestration.relation_link_validator import validate_relation_links
from masm.schemas.agents import PerceptionResult, RelationLinkDecision
from masm.schemas.api import AddRequest, Message
from masm.services.deletion_service import DeletionReport, DeletionService
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryCandidate, MemoryDraft


class _HistoryBuilder(Protocol):
    def build(
        self, request: AddRequest, perception: PerceptionResult
    ) -> list[MemoryCandidate]: ...


class _RelationLinker(Protocol):
    def propose(
        self, new_memory: MemoryDraft, history: Sequence[MemoryCandidate]
    ) -> RelationLinkDecision: ...


@dataclass
class LinkStageRecorder:
    """Keep private comparison state while exposing aggregate evidence only."""

    _user_id: str = ""
    _expected_target_id: UUID | None = None
    history_count: int = 0
    expected_target_in_history: bool = False
    linker_called: bool = False
    proposal_count: int = 0
    expected_target_proposed: bool = False
    accepted_count: int = 0
    expected_target_accepted: bool = False
    failure_category: str | None = None

    def begin(self, *, user_id: str, expected_target_id: UUID | None) -> None:
        """Reset one Add step; identifiers remain private and are never serialized."""
        self._user_id = user_id
        self._expected_target_id = expected_target_id
        self.history_count = 0
        self.expected_target_in_history = False
        self.linker_called = False
        self.proposal_count = 0
        self.expected_target_proposed = False
        self.accepted_count = 0
        self.expected_target_accepted = False
        self.failure_category = None

    def observe_history(self, history: Sequence[MemoryCandidate]) -> None:
        self.history_count = len(history)
        self.expected_target_in_history = self._expected_target_id is not None and any(
            candidate.memory_id == self._expected_target_id for candidate in history
        )

    def observe_decision(
        self,
        new_memory: MemoryDraft,
        history: Sequence[MemoryCandidate],
        decision: RelationLinkDecision,
    ) -> None:
        self.linker_called = True
        self.proposal_count = len(decision.links)
        self.expected_target_proposed = self._expected_target_id is not None and any(
            proposal.target_memory_id == self._expected_target_id
            for proposal in decision.links
        )
        accepted = validate_relation_links(
            self._user_id,
            decision,
            new_memory.original_text,
            history,
        )
        self.accepted_count = len(accepted)
        self.expected_target_accepted = self._expected_target_id is not None and any(
            relation.target_id == self._expected_target_id for relation in accepted
        )

    def safe_row(self, *, step: int, persisted_adjacency: bool) -> dict[str, object]:
        """Return the fixed, content-free diagnostic contract."""
        return {
            "case": "relation_link_stage",
            "step": step,
            "history_count": self.history_count,
            "expected_target_in_history": self.expected_target_in_history,
            "linker_called": self.linker_called,
            "proposal_count": self.proposal_count,
            "expected_target_proposed": self.expected_target_proposed,
            "accepted_count": self.accepted_count,
            "expected_target_accepted": self.expected_target_accepted,
            "persisted_adjacency": persisted_adjacency,
            "failure_category": self.failure_category,
        }


class ObservedHistoryBuilder:
    """Delegate history construction and retain only safe aggregate observations."""

    def __init__(self, delegate: _HistoryBuilder, recorder: LinkStageRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def build(
        self, request: AddRequest, perception: PerceptionResult
    ) -> list[MemoryCandidate]:
        history = self._delegate.build(request, perception)
        self._recorder.observe_history(history)
        return history


class ObservedRelationLinker:
    """Delegate relation linking and classify its output without logging evidence."""

    def __init__(self, delegate: _RelationLinker, recorder: LinkStageRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def propose(
        self, new_memory: MemoryDraft, history: Sequence[MemoryCandidate]
    ) -> RelationLinkDecision:
        self._recorder.linker_called = True
        try:
            decision = self._delegate.propose(new_memory, history)
            self._recorder.observe_decision(new_memory, history, decision)
            return decision
        except Exception as exc:
            self._recorder.failure_category = type(exc).__name__
            raise


def _print_row(output: TextIO, row: dict[str, object]) -> None:
    print(json.dumps(row, sort_keys=True), file=output)


def _stored_context_id(
    repository: MemoryRepository, user_id: str, request_id: str
) -> UUID:
    contexts = repository.context_candidates_for_requests(user_id, [request_id])
    if len(contexts) != 1:
        raise RuntimeError("stored context contract")
    return contexts[0].memory_id


def _is_adjacent(
    repository: MemoryRepository,
    user_id: str,
    source_id: UUID,
    expected_target_id: UUID,
) -> bool:
    return any(
        candidate.memory_id == expected_target_id
        for candidate in repository.related(user_id, [source_id], 32)
    )


def _cleanup_rows(
    deletion: DeletionService,
    user_id: str,
    run_ids: Sequence[str],
) -> dict[str, object]:
    reports: list[DeletionReport] = []
    error_count = 0
    for _ in range(3):
        for run_id in run_ids:
            try:
                reports.append(deletion.delete_run(run_id, user_id=user_id))
            except Exception:
                error_count += 1
    final = reports[-len(run_ids) :] if len(reports) >= len(run_ids) else []
    complete = (
        error_count == 0
        and len(final) == len(run_ids)
        and all(report.complete for report in final)
        and sum(report.memories_deleted for report in final) == 0
        and sum(report.sources_deleted for report in final) == 0
    )
    return {
        "case": "cleanup",
        "registered_run_count": len(run_ids),
        "cleanup_attempt_error_count": error_count,
        "complete": complete,
        "passed": complete,
    }


def run_diagnostic(*, rounds: int, output: TextIO) -> int:
    """Run bounded synthetic chains in-process against the configured runtime."""
    settings = Settings.from_env()
    settings.validate_runtime()
    if settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM:
        raise ValueError("official runtime profile required")

    application = create_app(settings)
    database = application.state.database
    runtime = application.state.runtime
    repository = MemoryRepository(database)
    deletion = DeletionService(repository, application.state.asset_store)
    pipeline = runtime.add_pipeline
    if pipeline is None or pipeline._relation_linker is None:  # noqa: SLF001
        runtime.close()
        database.engine.dispose()
        raise ValueError("relation linker runtime required")

    recorder = LinkStageRecorder()
    original_history = pipeline._history_builder  # noqa: SLF001
    original_linker = pipeline._relation_linker  # noqa: SLF001
    pipeline_any = cast(Any, pipeline)
    pipeline_any._history_builder = ObservedHistoryBuilder(original_history, recorder)
    pipeline_any._relation_linker = ObservedRelationLinker(original_linker, recorder)

    all_passed = True
    try:
        for round_number in range(1, rounds + 1):
            tag = uuid4().hex[:12]
            user_id = f"relation-link-diagnostic-{tag}"
            root = f"Root-{tag}"
            bridge = f"Bridge-{tag}"
            leaf = f"Leaf-{tag}"
            terminal = f"Terminal-{tag}"
            run_ids = [f"{user_id}-{index}" for index in range(3)]
            registered: list[str] = []
            texts = (
                f"The registered chain {root} continues through {bridge}.",
                f"For the same registered chain, {bridge} continues through {leaf}.",
                f"The terminal record associated with {leaf} is {terminal}.",
            )
            try:
                expected_target_id: UUID | None = None
                for index, (run_id, content) in enumerate(
                    zip(run_ids, texts, strict=True)
                ):
                    registered.append(run_id)
                    recorder.begin(
                        user_id=user_id,
                        expected_target_id=expected_target_id,
                    )
                    application.state.add_service.add(
                        AddRequest(
                            request_id=run_id,
                            user_id=user_id,
                            session_id=f"{user_id}-session-{index}",
                            messages=[Message(role="user", content=content)],
                        )
                    )
                    current_id = _stored_context_id(repository, user_id, run_id)
                    if expected_target_id is not None:
                        persisted = _is_adjacent(
                            repository,
                            user_id,
                            current_id,
                            expected_target_id,
                        )
                        row = recorder.safe_row(
                            step=index,
                            persisted_adjacency=persisted,
                        )
                        row["round"] = round_number
                        _print_row(output, row)
                        all_passed &= persisted
                    expected_target_id = current_id
            except Exception:
                all_passed = False
                _print_row(
                    output,
                    {
                        "case": "diagnostic",
                        "round": round_number,
                        "failure_category": "internal_error",
                        "passed": False,
                    },
                )
            finally:
                cleanup = _cleanup_rows(deletion, user_id, registered)
                cleanup["round"] = round_number
                _print_row(output, cleanup)
                all_passed &= cleanup["passed"] is True
    finally:
        runtime.close()
        database.engine.dispose()
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit content-free Add-time relation-link diagnostics"
    )
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args([] if argv is None else argv)
    if not 1 <= args.rounds <= 5:
        _print_row(
            sys.stdout,
            {
                "case": "preflight",
                "failure_category": "configuration",
                "passed": False,
            },
        )
        return 2
    try:
        return run_diagnostic(rounds=args.rounds, output=sys.stdout)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
