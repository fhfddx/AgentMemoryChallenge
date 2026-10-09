"""Exercise the deployed selector with synthetic evidence only."""

import json
import sys
from typing import TextIO
from uuid import UUID

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import RankedEvidence


def run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int:
    """Check one supported fact and one same-entity unsupported question."""
    known_id, distractor_id = UUID(int=1), UUID(int=2)
    ranked = (
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
    selector = EvidenceSelector(
        llm,
        max_candidates=settings.selector_max_candidates,
        max_selected=settings.selector_max_selected,
        max_chars_per_candidate=settings.selector_max_chars_per_candidate,
        timeout_seconds=settings.selector_timeout_seconds,
    )
    checks: tuple[tuple[str, str, set[UUID]], ...] = (
        ("positive", "Where did Nora store the amber key?", {known_id}),
        ("unrelated", "What is Nora's passport number?", set()),
    )
    all_passed = True
    for name, question, expected_ids in checks:
        result = selector.select(question, None, ranked, {known_id, distractor_id})
        selected_ids = {item.memory_id for item in result.evidence}
        passed = (
            selected_ids == expected_ids
            and not result.fallback
            and result.abstained == (name == "unrelated")
        )
        all_passed &= passed
        print(
            json.dumps(
                {
                    "case": name,
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
    return 0 if all_passed else 1


def main() -> int:
    """Run the probe only when the deployed official selector is enabled."""
    try:
        settings = Settings.from_env()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.selector_enabled
        ):
            print(json.dumps({"error": "selector_disabled"}))
            return 2
        settings.validate_runtime()
        llm = OpenAICompatibleLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=settings.model_max_attempts,
        )
        try:
            return run_probe(llm, settings, sys.stdout)
        finally:
            close = getattr(llm, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        print(json.dumps({"error": "probe_failed", "category": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
