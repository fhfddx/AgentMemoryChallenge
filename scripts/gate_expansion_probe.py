"""Probe deterministic cross-source expansion after the selector acts as a gate."""

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO
from uuid import UUID

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.retrieval.evidence_selector import EvidenceSelector, SelectionResult
from masm.retrieval.reranker import RankedEvidence


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    contents: tuple[str, ...]
    strong_indices: frozenset[int]
    expected_indices: frozenset[int]


def _crowded(prefix: str, count: int) -> tuple[str, ...]:
    return tuple(f"{prefix} unrelated archive marker {index}." for index in range(count))


CASES = (
    Case(
        "cross_source_chain",
        "Which city hosts the workshop Nora registered for?",
        (
            "Nora registered for the Zephyr workshop.",
            "The Zephyr workshop is hosted in Lisbon.",
        ),
        frozenset({0, 1}),
        frozenset({0, 1}),
    ),
    Case(
        "direct_with_high_rank_distractor",
        "Where did I store the amber key?",
        (
            "A colleague archived an unrelated cobalt ticket in Oslo.",
            "I stored the amber key in drawer three.",
        ),
        frozenset({1}),
        frozenset({1}),
    ),
    Case(
        "direct_two_source_control",
        "What are the Atlas code name and review date?",
        (
            "The Atlas project code name is Zephyr.",
            "The Atlas project review date is April 18.",
        ),
        frozenset({0, 1}),
        frozenset({0, 1}),
    ),
    Case(
        "disconnected_bridge",
        "Which city hosts the workshop Nora registered for?",
        (
            "Nora registered for the Zephyr workshop.",
            "The Aurora workshop is hosted in Lisbon.",
        ),
        frozenset({0, 1}),
        frozenset(),
    ),
    Case(
        "wrong_subject",
        "Which city did I move to?",
        (
            "My colleague moved to Porto.",
            "I have not shared my own destination.",
        ),
        frozenset({0, 1}),
        frozenset(),
    ),
    Case(
        "same_entity_abstention",
        "What is Nora's passport number?",
        (
            "Nora prefers blue notebooks.",
            "Nora visited Lisbon in April.",
        ),
        frozenset({0, 1}),
        frozenset(),
    ),
    Case(
        "crowded_cross_source_chain",
        "Which city hosts the workshop Nora registered for?",
        _crowded("crowded-positive", 14)
        + (
            "Nora registered for the Zephyr workshop.",
            "The Zephyr workshop is hosted in Lisbon.",
        ),
        frozenset({14, 15}),
        frozenset({14, 15}),
    ),
    Case(
        "crowded_abstention",
        "What is Nora's passport number?",
        _crowded("crowded-negative", 32),
        frozenset(range(32)),
        frozenset(),
    ),
)


def _ranked(case: Case) -> list[RankedEvidence]:
    return [
        RankedEvidence(
            memory_id=UUID(int=index + 1),
            user_id="synthetic-gate-expansion",
            content=content,
            score=1.0 - index / 100.0,
            rank=index + 1,
            request_id=f"{case.name}-source-{index}",
            granularity="message",
            source_position=0,
            metadata=(
                {"lexical": 1.0 - index / 100.0, "text_vector": 0.9}
                if index in case.strong_indices
                else {"text_vector": 0.4}
            ),
        )
        for index, content in enumerate(case.contents)
    ]


def _source_key(item: RankedEvidence) -> str:
    return item.request_id or str(item.memory_id)


def _clean_positive(result: SelectionResult) -> bool:
    return (
        result.failure_category == "none"
        and not result.fallback
        and not result.abstained
        and result.evidence_state == "sufficient"
    )


def _next_distinct(
    result: SelectionResult,
    ranked: Sequence[RankedEvidence],
    strong_ids: frozenset[UUID],
) -> tuple[RankedEvidence, ...]:
    if not _clean_positive(result) or result.selected_source_count != 1:
        return result.evidence
    selected = list(result.evidence)
    selected_ids = {item.memory_id for item in selected}
    selected_sources = {_source_key(item) for item in selected}
    for item in ranked:
        if (
            item.memory_id in strong_ids
            and item.memory_id not in selected_ids
            and _source_key(item) not in selected_sources
        ):
            selected.append(item)
            break
    return tuple(selected)


def _top2_distinct(
    result: SelectionResult,
    ranked: Sequence[RankedEvidence],
    strong_ids: frozenset[UUID],
) -> tuple[RankedEvidence, ...]:
    if not _clean_positive(result) or result.selected_source_count != 1:
        return result.evidence
    selected: list[RankedEvidence] = []
    sources: set[str] = set()
    for item in ranked:
        source = _source_key(item)
        if item.memory_id not in strong_ids or source in sources:
            continue
        selected.append(item)
        sources.add(source)
        if len(selected) == 2:
            return tuple(selected)
    return result.evidence


def _indices(
    evidence: Sequence[RankedEvidence], ranked: Sequence[RankedEvidence]
) -> frozenset[int]:
    by_id = {item.memory_id: index for index, item in enumerate(ranked)}
    return frozenset(by_id[item.memory_id] for item in evidence)


def _row(
    case: Case,
    variant: str,
    evidence: Sequence[RankedEvidence],
    result: SelectionResult,
    ranked: Sequence[RankedEvidence],
) -> dict[str, object]:
    indices = _indices(evidence, ranked)
    passed = (
        indices == case.expected_indices
        and result.failure_category == "none"
        and not result.fallback
        and (bool(evidence) != result.abstained)
    )
    return {
        "case": case.name,
        "variant": variant,
        "passed": passed,
        "evidence_state": result.evidence_state,
        "abstained": not evidence,
        "fallback": result.fallback,
        "failure_category": result.failure_category,
        "candidate_count": result.candidate_count,
        "selected_count": len(evidence),
        "selected_source_count": len({_source_key(item) for item in evidence}),
    }


def run_probe(
    llm: StructuredLLM,
    output: TextIO,
    *,
    cases: Sequence[Case] = CASES,
) -> int:
    """Run each fixed case once and compare two deterministic expansion policies."""
    selector = EvidenceSelector(llm)
    rows: list[dict[str, object]] = []
    failure_count = 0
    for case in cases:
        ranked = _ranked(case)
        strong_ids = frozenset(
            ranked[index].memory_id for index in case.strong_indices
        )
        result = selector.select(case.question, None, ranked, strong_ids)
        if result.failure_category != "none":
            failure_count += 1
        variants = (
            ("current", result.evidence),
            ("next_distinct", _next_distinct(result, ranked, strong_ids)),
            ("top2_distinct", _top2_distinct(result, ranked, strong_ids)),
        )
        for variant, evidence in variants:
            row = _row(case, variant, evidence, result, ranked)
            rows.append(row)
            print(json.dumps(row, sort_keys=True), file=output, flush=True)

    def variant_rows(name: str) -> list[dict[str, object]]:
        return [row for row in rows if row["variant"] == name]

    current_rows = variant_rows("current")
    current_passed = sum(row["passed"] is True for row in current_rows)
    summary: dict[str, object] = {
        "case": "summary",
        "case_count": len(cases),
        "current_passed": current_passed,
        "failure_count": failure_count,
    }
    eligible_variants: list[str] = []
    for variant in ("next_distinct", "top2_distinct"):
        candidate_rows = variant_rows(variant)
        passed = sum(row["passed"] is True for row in candidate_rows)
        regressions = sum(
            current["passed"] is True and candidate["passed"] is not True
            for current, candidate in zip(current_rows, candidate_rows, strict=True)
        )
        targets_repaired = all(
            row["passed"] is True and row["selected_source_count"] == 2
            for row in candidate_rows
            if row["case"] in {"cross_source_chain", "crowded_cross_source_chain"}
        )
        summary[f"{variant}_passed"] = passed
        summary[f"{variant}_regressions"] = regressions
        if (
            passed == len(cases)
            and regressions == 0
            and passed > current_passed
            and targets_repaired
            and failure_count == 0
        ):
            eligible_variants.append(variant)
    summary["eligible_variants"] = eligible_variants
    summary["candidate_eligible"] = bool(eligible_variants)
    print(json.dumps(summary, sort_keys=True), file=output, flush=True)
    return 0 if eligible_variants else 1


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
