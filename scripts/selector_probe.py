"""Exercise the deployed selector with synthetic evidence only."""

import argparse
import json
import logging
import sys
from typing import TextIO
from uuid import UUID

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import RankedEvidence


def run_probe(
    llm: StructuredLLM,
    settings: Settings,
    output: TextIO,
    *,
    extended_only: bool = False,
    crowded_only: bool = False,
) -> int:
    """Check direct, abstention, option-overlap, and multi-source boundaries."""
    known_id, distractor_id = UUID(int=1), UUID(int=2)
    direct_ranked = (
        RankedEvidence(
            memory_id=known_id,
            user_id="synthetic-selector-probe",
            content="Nora stored the amber key in drawer three.",
            score=1.0,
            rank=1,
            granularity="message",
            request_id="synthetic-source-1",
        ),
        RankedEvidence(
            memory_id=distractor_id,
            user_id="synthetic-selector-probe",
            content="The training room has a blue clock.",
            score=0.5,
            rank=2,
            granularity="message",
            request_id="synthetic-source-2",
        ),
    )
    project_id, review_id = UUID(int=3), UUID(int=4)
    multi_source_ranked = (
        RankedEvidence(
            memory_id=project_id,
            user_id="synthetic-selector-probe",
            content="The Atlas project uses code name Zephyr.",
            score=1.0,
            rank=1,
            granularity="message",
            request_id="synthetic-source-3",
        ),
        RankedEvidence(
            memory_id=review_id,
            user_id="synthetic-selector-probe",
            content="The review for the Atlas project is scheduled on April 18.",
            score=0.9,
            rank=2,
            granularity="message",
            request_id="synthetic-source-4",
        ),
    )
    crowded_ranked = (
        *(
            RankedEvidence(
                memory_id=UUID(int=100 + index),
                user_id="synthetic-selector-probe",
                content=(
                    f"The Atlas project archived document {index} "
                    f"in cabinet {index % 10}."
                ),
                score=1.0 - (index * 0.01),
                rank=index + 1,
                granularity="message",
                request_id=f"synthetic-crowded-source-{index}",
            )
            for index in range(30)
        ),
        RankedEvidence(
            memory_id=project_id,
            user_id="synthetic-selector-probe",
            content="The Atlas project uses code name Zephyr.",
            score=0.69,
            rank=31,
            granularity="message",
            request_id="synthetic-crowded-source-30",
        ),
        RankedEvidence(
            memory_id=review_id,
            user_id="synthetic-selector-probe",
            content="The review for the Atlas project is scheduled on April 18.",
            score=0.68,
            rank=32,
            granularity="message",
            request_id="synthetic-crowded-source-31",
        ),
    )
    deep_crowded_ranked = (
        *(
            RankedEvidence(
                memory_id=UUID(int=200 + index),
                user_id="synthetic-selector-probe",
                content=(
                    f"The Atlas project archived report {index} "
                    f"in cabinet {index % 10}."
                ),
                score=1.0 - (index * 0.01),
                rank=index + 1,
                granularity="message",
                request_id=f"synthetic-deep-source-{index}",
            )
            for index in range(46)
        ),
        RankedEvidence(
            memory_id=project_id,
            user_id="synthetic-selector-probe",
            content="The Atlas project uses code name Zephyr.",
            score=0.54,
            rank=47,
            granularity="message",
            request_id="synthetic-deep-source-46",
        ),
        RankedEvidence(
            memory_id=review_id,
            user_id="synthetic-selector-probe",
            content="The review for the Atlas project is scheduled on April 18.",
            score=0.53,
            rank=48,
            granularity="message",
            request_id="synthetic-deep-source-47",
        ),
    )
    selector = EvidenceSelector(
        llm,
        max_candidates=settings.selector_max_candidates,
        max_selected=settings.selector_max_selected,
        max_chars_per_candidate=settings.selector_max_chars_per_candidate,
        timeout_seconds=settings.selector_timeout_seconds,
    )
    checks: tuple[
        tuple[str, str, list[str] | None, tuple[RankedEvidence, ...], set[UUID]], ...
    ] = (
        ("positive", "Where did Nora store the amber key?", None, direct_ranked, {known_id}),
        ("unrelated", "What is Nora's passport number?", None, direct_ranked, set()),
        (
            "option_overlap_abstention",
            "What is Nora's passport number?",
            ["drawer three", "P-4821"],
            direct_ranked,
            set(),
        ),
        (
            "multi_source",
            "What are the code name and review date of the Atlas project?",
            None,
            multi_source_ranked,
            {project_id, review_id},
        ),
    )
    crowded_checks: tuple[
        tuple[str, str, list[str] | None, tuple[RankedEvidence, ...], set[UUID]], ...
    ] = (
        (
            "crowded_abstention",
            "What is the Atlas project's budget authorization code?",
            ["cabinet 3", "P-4821"],
            crowded_ranked,
            set(),
        ),
        (
            "crowded_multi_source",
            "What are the code name and review date of the Atlas project?",
            None,
            crowded_ranked,
            {project_id, review_id},
        ),
        (
            "deep_crowded_multi_source",
            "What are the code name and review date of the Atlas project?",
            None,
            deep_crowded_ranked,
            {project_id, review_id},
        ),
    )
    if crowded_only:
        checks = crowded_checks
    elif extended_only:
        checks = checks[2:]
    all_passed = True
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for name, question, options, ranked, expected_ids in checks:
            result = selector.select(
                question,
                options,
                ranked,
                {item.memory_id for item in ranked},
            )
            selected_ids = {item.memory_id for item in result.evidence}
            passed = (
                selected_ids == expected_ids
                and not result.fallback
                and result.abstained == (not expected_ids)
            )
            all_passed &= passed
            print(
                json.dumps(
                    {
                        "case": name,
                        "candidate_source_count": result.source_count,
                        "selected_count": len(result.evidence),
                        "selected_source_count": result.selected_source_count,
                        "fallback": result.fallback,
                        "abstained": result.abstained,
                        "failure_category": result.failure_category,
                        "passed": passed,
                    },
                    sort_keys=True,
                ),
                file=output,
            )
    finally:
        provider_logger.disabled = was_disabled
    return 0 if all_passed else 1


def _report_failure(case: str, category: str) -> None:
    print(
        json.dumps(
            {
                "case": case,
                "candidate_source_count": 0,
                "selected_count": 0,
                "selected_source_count": 0,
                "fallback": False,
                "abstained": False,
                "failure_category": category,
                "passed": False,
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Run the probe only when the deployed official selector is enabled."""
    parser = argparse.ArgumentParser(description="Run synthetic selector diagnostics")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--extended-only", action="store_true")
    mode.add_argument("--crowded-only", action="store_true")
    args = parser.parse_args([] if argv is None else argv)
    try:
        settings = Settings.from_env()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.selector_enabled
        ):
            _report_failure("preflight", "configuration")
            return 2
        settings.validate_runtime()
    except Exception:
        _report_failure("preflight", "configuration")
        return 2
    try:
        llm = OpenAICompatibleLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=settings.model_max_attempts,
        )
        try:
            return run_probe(
                llm,
                settings,
                sys.stdout,
                extended_only=args.extended_only,
                crowded_only=args.crowded_only,
            )
        finally:
            close = getattr(llm, "close", None)
            if callable(close):
                close()
    except Exception:
        _report_failure("probe", "internal_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
