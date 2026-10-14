"""Paired synthetic source-context experiment, without database access."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO
from uuid import UUID

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.source_context import SourceContextEnricher
from masm.storage.types import SourceMessageSnapshot


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    sources: tuple[tuple[tuple[str, str], ...], ...]
    positions: tuple[int, ...]
    required: tuple[str, ...]  # Empty means no answer is documented.


CASES = (
    Case("elliptical_confirmation", "Which city did I move to?", ((
        ("assistant", "Did you move to Porto?"), ("user", "Yes, last week."),
    ),), (1,), ("Did you move to Porto?", "Yes, last week.")),
    Case("forward_correction", "What color bicycle did I buy?", ((
        ("assistant", "Did you buy the blue bicycle?"),
        ("user", "No, I bought the red one."),
    ),), (0,), ("Did you buy the blue bicycle?", "No, I bought the red one.")),
    Case("question_only", "Which city did I move to?", ((
        ("assistant", "Did you move to Porto?"),
        ("user", "I have not told you where I moved."),
    ),), (0,), ()),
    Case("assistant_suggestion", "Which workshop did I register for?", ((
        ("assistant", "You could register for the Zephyr workshop in Lisbon."),
        ("user", "I have not decided or registered for anything."),
    ),), (0,), ()),
    Case("accepted_suggestion", "Which workshop did I register for?", ((
        ("assistant", "You could register for the Zephyr workshop in Lisbon."),
        ("user", "I registered for that workshop this morning."),
    ),), (1,), ("Zephyr workshop", "I registered for that workshop")),
    Case("cross_source_context", "Which city hosts the workshop I registered for?", (
        (("assistant", "Did you register for the Zephyr workshop?"), ("user", "Yes.")),
        (("user", "The Zephyr workshop is held in Lisbon."),),
    ), (1, 0), ("Did you register for the Zephyr workshop?", "Yes.", "held in Lisbon")),
    Case("wrong_subject", "Which city did I move to?", ((
        ("user", "My colleague moved to Porto. I have not shared my own destination."),
    ),), (0,), ()),
    Case("direct_control", "Where did I store the amber key?", ((
        ("user", "I stored the amber key in drawer three."),
    ),), (0,), ("I stored the amber key in drawer three.",)),
    Case("two_source_control", "What is the project code name and review date?", (
        (("user", "The project code name is Zephyr."),),
        (("user", "The project review date is April 18."),),
    ), (0, 0), ("code name is Zephyr", "review date is April 18")),
    Case("chinese_context", "我报名的课程叫什么？", ((
        ("assistant", "你报名的是陶艺课吗？"), ("user", "对，就是那个。"),
    ),), (1,), ("你报名的是陶艺课吗？", "对，就是那个。")),
)


class SyntheticSources:
    """In-memory immutable sources; no database or production data is accessed."""

    def __init__(self, case: Case) -> None:
        self.snapshots = {
            (f"source-{source}", position): SourceMessageSnapshot(text, role=role)
            for source, messages in enumerate(case.sources)
            for position, (role, text) in enumerate(messages)
        }

    def source_messages_for_positions(
        self, user_id: str, positions: Sequence[tuple[str, int]],
    ) -> dict[tuple[str, int], SourceMessageSnapshot]:
        if user_id != "synthetic-source-context":
            return {}
        return {key: self.snapshots[key] for key in positions if key in self.snapshots}


def _ranked(case: Case) -> list[RankedEvidence]:
    return [RankedEvidence(
        memory_id=UUID(int=index + 1), user_id="synthetic-source-context",
        content=case.sources[index][position][1], score=1.0 / (index + 1), rank=index + 1,
        request_id=f"source-{index}", granularity="message", source_position=position,
    ) for index, position in enumerate(case.positions)]


def run_probe(
    llm: StructuredLLM, output: TextIO, *, reverse: bool = False,
) -> int:
    """Same prompt, candidates and model; only source context differs.

    Passing validates evidence availability/selection, not final-answer correctness
    or an official score. The gate is fixed before any real-provider run.
    """
    selector = EvidenceSelector(llm)
    baseline_passed = candidate_passed = regressions = 0
    for case in CASES:
        original = _ranked(case)
        enriched = SourceContextEnricher(SyntheticSources(case)).enrich(
            "synthetic-source-context", original,
        )
        outcomes = {}
        variants = [("baseline", original), ("context", enriched)]
        for variant, evidence in reversed(variants) if reverse else variants:
            result = selector.select(case.question, None, evidence, {e.memory_id for e in evidence})
            text = "\n".join(str(e.source_context or e.content) for e in result.evidence)
            clean = result.failure_category == "none" and not result.fallback
            passed = clean and (
                result.evidence_state == "sufficient" and not result.abstained
                and all(fragment in text for fragment in case.required)
                if case.required else result.abstained and not result.evidence
            )
            outcomes[variant] = passed
            print(json.dumps({
                "case": case.name, "variant": variant, "passed": passed,
                "evidence_state": result.evidence_state, "abstained": result.abstained,
                "fallback": result.fallback, "failure_category": result.failure_category,
                "selected_count": len(result.evidence),
                "selected_source_count": result.selected_source_count,
            }, sort_keys=True), file=output, flush=True)
        baseline_passed += outcomes["baseline"]
        candidate_passed += outcomes["context"]
        regressions += outcomes["baseline"] and not outcomes["context"]
    eligible = (candidate_passed == len(CASES) and regressions == 0
                and candidate_passed > baseline_passed)
    print(json.dumps({
        "case": "summary", "case_count": len(CASES), "baseline_passed": baseline_passed,
        "candidate_passed": candidate_passed, "regressions": regressions,
        "candidate_eligible": eligible,
    }, sort_keys=True), file=output, flush=True)
    return 0 if eligible else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reverse", action="store_true", help="Run context first in each pair")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    settings = Settings.from_env()
    if settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM:
        print('{"case":"configuration","passed":false}')
        return 2
    try:
        settings.validate_runtime()
        llm = OpenAICompatibleLLM(
            model=settings.llm_model, base_url=settings.llm_base_url,
            api_key=settings.llm_api_key, timeout_seconds=settings.model_timeout_seconds,
            max_attempts=1,
        )
        try:
            return run_probe(llm, sys.stdout, reverse=args.reverse)
        finally:
            llm.close()
    except Exception:
        print('{"case":"probe_error","passed":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
