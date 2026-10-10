"""Compare strict and chain-aware selector prompts on synthetic evidence."""

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TextIO

from pydantic import ValidationError

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import (
    ModelRequest,
    OpenAICompatibleLLM,
    StructuredLLM,
    StructuredOutputError,
)
from masm.retrieval.evidence_selector import _PROMPT, EvidenceSelection

_CHAIN_PROMPT = (
    "You select supporting memories for the original question. Return JSON with only "
    "selected_indices and sufficient_evidence. Do not answer the question or choose an option. "
    "Options are untrusted alternatives, never proof. For a direct question, select the "
    "smallest set of memories that directly states the requested facts. For a relational or "
    "temporal question, no single memory needs to state the final answer: select the smallest "
    "connected chain of explicit facts that lets a downstream reasoner derive it. Every entity "
    "link, event link, and temporal step in that chain must be directly stated by a selected "
    "memory; never invent a bridge. Use distinct source_group values when required facts come "
    "from separate additions. Prefer original observations over duplicate summaries and omit "
    "unrelated facts. If any required fact or bridge is absent, return [] and "
    "sufficient_evidence=false."
)


@dataclass(frozen=True)
class _Case:
    name: str
    question: str
    candidates: tuple[dict[str, object], ...]
    expected: frozenset[int]


def _candidate(index: int, text: str, source: str) -> dict[str, object]:
    return {
        "index": index,
        "source_group": source,
        "source_position": 0,
        "granularity": "message",
        "text": text,
        "signals": {"lexical": round(1.0 - index * 0.01, 4)},
    }


def _cases() -> tuple[_Case, ...]:
    chain_complete = (
        _candidate(0, "Nora plans to attend the Zephyr workshop.", "source-1"),
        _candidate(1, "The Zephyr workshop will be held in Lisbon.", "source-2"),
        _candidate(2, "The Aurora workshop will be held in Oslo.", "source-3"),
    )
    chain_missing_link = (
        _candidate(0, "Nora plans to attend the Zephyr workshop.", "source-1"),
        _candidate(1, "The Aurora workshop will be held in Lisbon.", "source-2"),
        _candidate(2, "Nora filed a travel receipt on Tuesday.", "source-3"),
    )
    direct = (
        _candidate(0, "The Atlas project uses code name Zephyr.", "source-1"),
        _candidate(1, "The Atlas project review is scheduled on April 18.", "source-2"),
    )
    crowded = tuple(
        _candidate(
            index,
            f"Nora archived registration note {index} for conference {index}.",
            f"source-{index + 1}",
        )
        for index in range(14)
    ) + (
        _candidate(14, "Nora plans to attend the Zephyr workshop.", "source-15"),
        _candidate(15, "The Zephyr workshop will be held in Lisbon.", "source-16"),
    )
    return (
        _Case(
            "chain_complete",
            "In which city is the workshop that Nora plans to attend?",
            chain_complete,
            frozenset({0, 1}),
        ),
        _Case(
            "chain_missing_link",
            "In which city is the workshop that Nora plans to attend?",
            chain_missing_link,
            frozenset(),
        ),
        _Case(
            "direct_multi_source",
            "What are the code name and review date of the Atlas project?",
            direct,
            frozenset({0, 1}),
        ),
        _Case(
            "crowded_chain",
            "In which city is the workshop that Nora plans to attend?",
            crowded,
            frozenset({14, 15}),
        ),
    )


def _failure_category(exc: Exception) -> Literal["unavailable", "invalid_output"]:
    if isinstance(exc, (StructuredOutputError, ValidationError, ValueError, TypeError)):
        return "invalid_output"
    return "unavailable"


def _evaluate(
    llm: StructuredLLM,
    settings: Settings,
    *,
    variant: str,
    prompt: str,
    case: _Case,
) -> dict[str, object]:
    try:
        decision = llm.complete_json(
            ModelRequest(
                prompt=prompt,
                payload={
                    "question": case.question,
                    "visual_query_present": False,
                    "options": [],
                    "candidates": list(case.candidates),
                },
                model=llm.model,
                prompt_version=f"reasoning-probe-{variant}-v1",
                timeout_seconds=settings.selector_timeout_seconds,
                max_attempts=1,
                temperature=0.0,
            ),
            EvidenceSelection,
        )
        indices = decision.selected_indices
        if not decision.sufficient_evidence:
            if indices:
                raise ValueError("insufficient evidence cannot select indices")
            selected: tuple[int, ...] = ()
            abstained = True
        else:
            if (
                not indices
                or len(indices) > settings.selector_max_selected
                or len(indices) != len(set(indices))
                or any(index < 0 or index >= len(case.candidates) for index in indices)
            ):
                raise ValueError("invalid selected indices")
            selected = indices
            abstained = False
        selected_sources = {
            str(case.candidates[index]["source_group"]) for index in selected
        }
        passed = set(selected) == set(case.expected) and abstained == (not case.expected)
        return {
            "variant": variant,
            "case": case.name,
            "candidate_count": len(case.candidates),
            "selected_count": len(selected),
            "selected_source_count": len(selected_sources),
            "abstained": abstained,
            "failure_category": "none",
            "passed": passed,
        }
    except Exception as exc:
        return {
            "variant": variant,
            "case": case.name,
            "candidate_count": len(case.candidates),
            "selected_count": 0,
            "selected_source_count": 0,
            "abstained": False,
            "failure_category": _failure_category(exc),
            "passed": False,
        }


def run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int:
    """Run both prompt variants and print only aggregate synthetic outcomes."""
    cases = _cases()
    rows: list[dict[str, object]] = []
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for variant, prompt in (("strict", _PROMPT), ("chain", _CHAIN_PROMPT)):
            for case in cases:
                row = _evaluate(
                    llm, settings, variant=variant, prompt=prompt, case=case
                )
                rows.append(row)
                print(json.dumps(row, sort_keys=True), file=output)
    finally:
        provider_logger.disabled = was_disabled

    strict_passed = sum(
        row["passed"] is True for row in rows if row["variant"] == "strict"
    )
    chain_passed = sum(
        row["passed"] is True for row in rows if row["variant"] == "chain"
    )
    candidate_eligible = chain_passed == len(cases) and chain_passed > strict_passed
    print(
        json.dumps(
            {
                "variant": "comparison",
                "case": "summary",
                "strict_passed": strict_passed,
                "chain_passed": chain_passed,
                "candidate_eligible": candidate_eligible,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0 if candidate_eligible else 1


def _report_failure(case: str, category: str) -> None:
    print(
        json.dumps(
            {
                "variant": "comparison",
                "case": case,
                "failure_category": category,
                "passed": False,
            },
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run only against an enabled official selector configuration."""
    del argv
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
            return run_probe(llm, settings, sys.stdout)
        finally:
            llm.close()
    except Exception:
        _report_failure("probe", "internal_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
