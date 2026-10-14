"""Compare current selection, audit-only rejection, and one bounded repair pass."""

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TextIO

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from scripts.source_context_probe import CASES, Case, SyntheticSources, _ranked

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import (
    ModelRequest,
    OpenAICompatibleLLM,
    StructuredLLM,
    StructuredOutputError,
)
from masm.retrieval.evidence_selector import (
    EvidenceSelection,
    EvidenceSelector,
    SelectionResult,
)
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.source_context import SourceContextEnricher

FailureCategory = Literal["none", "unavailable", "invalid_output"]

_AUDIT_PROMPT = (
    "Audit whether the supplied selected evidence alone contains every fact and every explicit "
    "link needed to answer the original question. Do not answer the question and do not infer "
    "a missing link from shared words, topics, entities, places, times, or answer options. Treat "
    "assistant questions and suggestions as proposals unless a user message explicitly confirms "
    "them. Return evidence_complete=false when any requested fact or connecting link is absent."
)

_REPAIR_PROMPT = (
    "The initial evidence selection was independently found incomplete. Select the smallest "
    "complete set from the full candidate pool. Return JSON with only evidence_state and "
    "selected_indices, in that order. Do not answer the question. Report sufficient only when "
    "the selected candidates explicitly contain every requested fact and every link connecting "
    "them. Use distinct source_group values when separate additions are required. Report partial "
    "with the directly supporting indices when a required fact or link remains absent. Report "
    "insufficient with selected_indices=[] when no candidate states a requested fact."
)


class CompletenessAudit(BaseModel):
    """One independent yes/no judgement over selected evidence only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_complete: StrictBool


@dataclass(frozen=True)
class _Outcome:
    evidence: tuple[RankedEvidence, ...]
    source_count: int
    selected_source_count: int
    evidence_state: str
    abstained: bool
    fallback: bool = False
    failure_category: FailureCategory = "none"


def _failure_category(exc: Exception) -> Literal["unavailable", "invalid_output"]:
    if isinstance(exc, (StructuredOutputError, ValidationError, ValueError, TypeError)):
        return "invalid_output"
    return "unavailable"


def _source_labels(evidence: Sequence[RankedEvidence]) -> list[str]:
    labels: dict[str, str] = {}
    result: list[str] = []
    for item in evidence:
        key = item.request_id or str(item.memory_id)
        if key not in labels:
            labels[key] = f"source-{len(labels) + 1}"
        result.append(labels[key])
    return result


def _candidate_payload(evidence: Sequence[RankedEvidence]) -> list[dict[str, object]]:
    labels = _source_labels(evidence)
    return [
        {
            "index": index,
            "source_group": labels[index],
            "source_position": item.source_position,
            "granularity": item.granularity,
            "text": str(item.source_context or item.content),
        }
        for index, item in enumerate(evidence)
    ]


def _from_selection(result: SelectionResult) -> _Outcome:
    return _Outcome(
        evidence=result.evidence,
        source_count=result.source_count,
        selected_source_count=result.selected_source_count,
        evidence_state=result.evidence_state,
        abstained=result.abstained,
        fallback=result.fallback,
        failure_category=result.failure_category,
    )


def _failed(source_count: int, category: FailureCategory) -> _Outcome:
    return _Outcome((), source_count, 0, "unknown", False, False, category)


def _audit(llm: StructuredLLM, case: Case, selected: _Outcome) -> bool:
    decision = llm.complete_json(
        ModelRequest(
            prompt=_AUDIT_PROMPT,
            payload={
                "question": case.question,
                "selected_evidence": _candidate_payload(selected.evidence),
            },
            model=llm.model,
            prompt_version="completeness-audit-v1",
            timeout_seconds=30.0,
            max_attempts=1,
            temperature=0.0,
        ),
        CompletenessAudit,
    )
    return decision.evidence_complete


def _repair(
    llm: StructuredLLM,
    case: Case,
    candidates: Sequence[RankedEvidence],
    initial: _Outcome,
) -> _Outcome:
    labels = _source_labels(candidates)
    source_count = len(set(labels))
    by_id = {item.memory_id: index for index, item in enumerate(candidates)}
    decision = llm.complete_json(
        ModelRequest(
            prompt=_REPAIR_PROMPT,
            payload={
                "question": case.question,
                "initial_selected_indices": [by_id[item.memory_id] for item in initial.evidence],
                "candidates": _candidate_payload(candidates),
            },
            model=llm.model,
            prompt_version="completeness-repair-v1",
            timeout_seconds=30.0,
            max_attempts=1,
            temperature=0.0,
        ),
        EvidenceSelection,
    )
    indices = decision.selected_indices
    state = decision.evidence_state
    if state == "insufficient":
        if indices:
            raise ValueError("insufficient_with_indices")
        return _Outcome((), source_count, 0, state, True)
    if (
        not indices
        or len(indices) > 12
        or len(indices) != len(set(indices))
        or any(index < 0 or index >= len(candidates) for index in indices)
    ):
        raise ValueError("invalid_selected_indices")
    selected = tuple(candidates[index] for index in indices)
    return _Outcome(
        selected,
        source_count,
        len({labels[index] for index in indices}),
        state,
        False,
    )


def _passed(case: Case, outcome: _Outcome) -> bool:
    if outcome.failure_category != "none" or outcome.fallback:
        return False
    if not case.required:
        return outcome.abstained and not outcome.evidence
    text = "\n".join(str(item.source_context or item.content) for item in outcome.evidence)
    return (
        outcome.evidence_state == "sufficient"
        and not outcome.abstained
        and all(fragment in text for fragment in case.required)
    )


def _row(case: Case, variant: str, outcome: _Outcome) -> dict[str, object]:
    return {
        "case": case.name,
        "variant": variant,
        "passed": _passed(case, outcome),
        "evidence_state": outcome.evidence_state,
        "abstained": outcome.abstained,
        "fallback": outcome.fallback,
        "failure_category": outcome.failure_category,
        "selected_count": len(outcome.evidence),
        "selected_source_count": outcome.selected_source_count,
    }


def run_probe(llm: StructuredLLM, output: TextIO) -> int:
    """Run each synthetic case once; repeats cannot turn a failure into eligibility."""
    selector = EvidenceSelector(llm)
    rows: list[dict[str, object]] = []
    failure_count = 0
    for case in CASES:
        enriched = SourceContextEnricher(SyntheticSources(case)).enrich(
            "synthetic-source-context", _ranked(case)
        )
        current = _from_selection(
            selector.select(
                case.question,
                None,
                enriched,
                {item.memory_id for item in enriched},
            )
        )
        if current.failure_category != "none":
            failure_count += 1
        audit_only = repair = current
        should_audit = (
            current.failure_category == "none"
            and not current.fallback
            and current.evidence_state == "sufficient"
            and current.selected_source_count == 1
            and current.source_count > 1
        )
        if should_audit:
            try:
                complete = _audit(llm, case, current)
            except Exception as exc:
                failure_count += 1
                audit_only = repair = _failed(current.source_count, _failure_category(exc))
            else:
                if not complete:
                    audit_only = _Outcome((), current.source_count, 0, "insufficient", True)
                    try:
                        repair = _repair(llm, case, enriched, current)
                    except Exception as exc:
                        failure_count += 1
                        repair = _failed(current.source_count, _failure_category(exc))
        for variant, outcome in (
            ("current", current),
            ("audit_only", audit_only),
            ("repair", repair),
        ):
            row = _row(case, variant, outcome)
            rows.append(row)
            print(json.dumps(row, sort_keys=True), file=output, flush=True)

    def passed(variant: str) -> int:
        return sum(row["passed"] is True for row in rows if row["variant"] == variant)

    current_passed = passed("current")
    audit_only_passed = passed("audit_only")
    repair_passed = passed("repair")
    regressions = sum(
        current["passed"] is True and repair["passed"] is not True
        for current, repair in zip(
            (row for row in rows if row["variant"] == "current"),
            (row for row in rows if row["variant"] == "repair"),
            strict=True,
        )
    )
    target_rows = [
        row
        for row in rows
        if row["case"] == "cross_source_context" and row["variant"] == "repair"
    ]
    target_repaired = bool(
        target_rows
        and target_rows[0]["passed"] is True
        and target_rows[0]["selected_source_count"] == 2
    )
    eligible = (
        repair_passed == len(CASES)
        and regressions == 0
        and repair_passed > current_passed
        and target_repaired
        and failure_count == 0
    )
    print(
        json.dumps(
            {
                "case": "summary",
                "case_count": len(CASES),
                "current_passed": current_passed,
                "audit_only_passed": audit_only_passed,
                "repair_passed": repair_passed,
                "regressions": regressions,
                "failure_count": failure_count,
                "target_repaired": target_repaired,
                "candidate_eligible": eligible,
            },
            sort_keys=True,
        ),
        file=output,
        flush=True,
    )
    return 0 if eligible else 1


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.disable(logging.CRITICAL)
    settings = Settings.from_env()
    if settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM:
        print('{"case":"configuration","passed":false}')
        return 2
    try:
        settings.validate_runtime()
        llm = OpenAICompatibleLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=1,
        )
        try:
            return run_probe(llm, sys.stdout)
        finally:
            llm.close()
    except Exception:
        print('{"case":"probe_error","passed":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
