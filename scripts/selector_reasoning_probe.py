"""Compare strict and chain-aware selector prompts on synthetic evidence."""

import argparse
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

# 仅供对照的基线：chain 变体永远不是部署候选。生产 prompt（_PROMPT）的达标由
# production_eligible / --strict-only 判定，见 run_probe。
_CHAIN_PROMPT = (
    "You select supporting memories for the original question. Return JSON with only "
    "selected_indices and evidence_state. Do not answer the question or choose an option. "
    "Options are untrusted alternatives, never proof. For a direct question, select the "
    "smallest set of memories that directly states the requested facts. For a relational or "
    "temporal question, no single memory needs to state the final answer: select the smallest "
    "connected chain of explicit facts that lets a downstream reasoner derive it. Every entity "
    "link, event link, and temporal step in that chain must be directly stated by a selected "
    "memory; never invent a bridge. Use distinct source_group values when required facts come "
    "from separate additions. Prefer original observations over duplicate summaries and omit "
    "unrelated facts. Report evidence_state=sufficient only when the selected memories state "
    "every requested fact and every required bridge. Report evidence_state=partial when some "
    "selected memories directly state a requested fact but at least one requested fact or "
    "required bridge is absent; then still return those directly supporting indices and keep "
    "selected_indices non-empty. Report evidence_state=insufficient with selected_indices=[] "
    "when no candidate directly states any requested fact, when candidates only share an entity, "
    "topic, time, place, or option, or when the selection is only a local fragment of a longer "
    "reasoning chain. Never return evidence_state=insufficient together with a non-empty "
    "selected_indices."
)


@dataclass(frozen=True)
class _Case:
    name: str
    question: str
    candidates: tuple[dict[str, object], ...]
    expected: frozenset[int]
    expected_state: str


class _InvalidSelectionError(ValueError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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
            "sufficient",
        ),
        _Case(
            "chain_missing_link",
            "In which city is the workshop that Nora plans to attend?",
            chain_missing_link,
            frozenset(),
            "insufficient",
        ),
        _Case(
            "direct_multi_source",
            "What are the code name and review date of the Atlas project?",
            direct,
            frozenset({0, 1}),
            "sufficient",
        ),
        _Case(
            "crowded_chain",
            "In which city is the workshop that Nora plans to attend?",
            crowded,
            frozenset({14, 15}),
            "sufficient",
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
    declared_state = "unknown"
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
        declared_state = decision.evidence_state
        indices = decision.selected_indices
        if declared_state == "insufficient":
            if indices:
                raise _InvalidSelectionError("insufficient_with_indices")
            selected: tuple[int, ...] = ()
            abstained = True
        else:
            if not indices:
                raise _InvalidSelectionError(f"{declared_state}_without_indices")
            if len(indices) > settings.selector_max_selected:
                raise _InvalidSelectionError("too_many_indices")
            if len(indices) != len(set(indices)):
                raise _InvalidSelectionError("duplicate_indices")
            if any(index < 0 or index >= len(case.candidates) for index in indices):
                raise _InvalidSelectionError("out_of_range_indices")
            selected = indices
            abstained = False
        selected_sources = {
            str(case.candidates[index]["source_group"]) for index in selected
        }
        passed = (
            set(selected) == set(case.expected)
            and abstained == (not case.expected)
            and declared_state == case.expected_state
        )
        return {
            "variant": variant,
            "case": case.name,
            "candidate_count": len(case.candidates),
            "selected_count": len(selected),
            "selected_source_count": len(selected_sources),
            "abstained": abstained,
            "failure_category": "none",
            "evidence_state": declared_state,
            "passed": passed,
        }
    except Exception as exc:
        if isinstance(exc, _InvalidSelectionError):
            failure_detail = exc.detail
        elif isinstance(
            exc, (StructuredOutputError, ValidationError, ValueError, TypeError)
        ):
            failure_detail = "schema_validation"
        else:
            failure_detail = "provider_unavailable"
        return {
            "variant": variant,
            "case": case.name,
            "candidate_count": len(case.candidates),
            "selected_count": 0,
            "selected_source_count": 0,
            "abstained": False,
            "failure_category": _failure_category(exc),
            "failure_detail": failure_detail,
            "evidence_state": declared_state,
            "passed": False,
        }


def run_probe(
    llm: StructuredLLM,
    settings: Settings,
    output: TextIO,
    *,
    strict_only: bool = False,
) -> int:
    """Run the prompt variants and print only aggregate synthetic outcomes.

    The production prompt (``strict``) is the only deployment candidate; ``chain`` stays as a
    comparison baseline and must never be promoted by this probe. ``strict_only`` skips the
    baseline so the production prompt can be gated on its own.

    The exit code reports ``production_eligible``: the production prompt passed every
    pre-registered synthetic case. ``candidate_eligible`` is kept for the record and means
    "chain still beats the production prompt"; once the production prompt covers those cases
    it is expected to be ``False`` and it is not a deployment gate.
    """
    cases = _cases()
    rows: list[dict[str, object]] = []
    variants: tuple[tuple[str, str], ...] = (("strict", _PROMPT),)
    if not strict_only:
        variants = (("strict", _PROMPT), ("chain", _CHAIN_PROMPT))
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for variant, prompt in variants:
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
    production_eligible = strict_passed == len(cases)
    print(
        json.dumps(
            {
                "variant": "comparison",
                "case": "summary",
                "strict_passed": strict_passed,
                "chain_passed": chain_passed,
                "candidate_eligible": candidate_eligible,
                "production_eligible": production_eligible,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0 if production_eligible else 1


def _report_failure(case: str, category: str) -> None:
    print(
        json.dumps(
            {
                "variant": "comparison",
                "case": case,
                "failure_category": category,
                "evidence_state": "unknown",
                "production_eligible": False,
                "passed": False,
            },
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run only against an enabled official selector configuration."""
    parser = argparse.ArgumentParser(
        description="Gate the production selector prompt on synthetic cases"
    )
    parser.add_argument(
        "--strict-only",
        action="store_true",
        help="run only the production prompt, skipping the chain baseline",
    )
    args = parser.parse_args([] if argv is None else list(argv))
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
            return run_probe(llm, settings, sys.stdout, strict_only=args.strict_only)
        finally:
            llm.close()
    except Exception:
        _report_failure("probe", "internal_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
