"""Test a second-pass subject/path audit without changing production Search."""

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from pydantic import BaseModel, ConfigDict, StrictBool

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import (
    ModelRequest,
    OpenAICompatibleLLM,
    StructuredLLM,
    StructuredOutputError,
)

_PROMPT_VERSION = "subject-path-audit-probe-v1"
_PROMPT = (
    "Audit whether the selected memories, and only those memories, fully support the "
    "original question. Return JSON with exactly question_subject_supported, "
    "all_required_links_supported, and requested_value_supported, in that order. "
    "First establish whether a selected memory refers to the subject asked about. "
    "Use meaning and explicit aliases or conversational context; never require an exact "
    "identifier string match. A complete chain about a different subject does not support "
    "the question subject. Then check every relation or temporal link needed from that "
    "subject to the answer. A shared topic, route type, or nearby memory is not a bridge. "
    "Finally check that the selected memories establish every requested concrete value; "
    "unknown or undocumented is not a value unless the question asks whether it is known. "
    "For a multi-part question, every requested part must be covered. Do not answer the "
    "question or infer a missing fact. Judge selected evidence alone, ignoring any facts "
    "that might exist in unselected memories."
)


class AuditDecision(BaseModel):
    """Independent semantic audit; the experiment derives acceptance from all fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_subject_supported: StrictBool
    all_required_links_supported: StrictBool
    requested_value_supported: StrictBool


@dataclass(frozen=True)
class _Case:
    name: str
    question: str
    selected_evidence: tuple[str, ...]
    expected_supported: bool


def _cases() -> tuple[_Case, ...]:
    dossier_chain = (
        "Records for Dossier-A are assigned to the transfer phrase Relay-Cobalt.",
        "Every record routed by Relay-Cobalt ultimately arrives at Compartment Ember.",
    )
    return (
        _Case(
            "wrong_subject_complete_chain",
            "Which compartment do records for the Isolated dossier ultimately reach?",
            dossier_chain,
            False,
        ),
        _Case(
            "connected_chain",
            "Which compartment do records for Dossier-A ultimately reach?",
            dossier_chain,
            True,
        ),
        _Case(
            "alias_chain",
            "Where do records for the Sapphire file ultimately arrive?",
            ("The Sapphire file is also recorded as Dossier-A.", *dossier_chain),
            True,
        ),
        _Case(
            "direct_two_attributes",
            "What are the code name and review date of the Atlas project?",
            (
                "The Atlas project uses code name Zephyr.",
                "The Atlas project review is scheduled on April 18.",
            ),
            True,
        ),
        _Case(
            "disconnected_bridge",
            "In which city is the workshop that Nora plans to attend?",
            (
                "Nora plans to attend the Zephyr workshop.",
                "The Aurora workshop will be held in Lisbon.",
            ),
            False,
        ),
        _Case(
            "missing_value",
            "Which compartment do records for the Isolated dossier reach?",
            ("The Isolated dossier's destination has not been documented.",),
            False,
        ),
        _Case(
            "paraphrased_direct",
            "Where did the speaker put the key used for their storage cabinet?",
            ("I placed the cabinet key in drawer three.",),
            True,
        ),
    )


def run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int:
    """Print only case-level boolean outcomes, never candidate or provider text."""
    passed_count = 0
    cases = _cases()
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for case in cases:
            try:
                decision = llm.complete_json(
                    ModelRequest(
                        prompt=_PROMPT,
                        payload={
                            "question": case.question,
                            "selected_evidence": [
                                {"index": index, "text": content}
                                for index, content in enumerate(case.selected_evidence)
                            ],
                        },
                        model=llm.model,
                        prompt_version=_PROMPT_VERSION,
                        timeout_seconds=settings.selector_timeout_seconds,
                        max_attempts=1,
                        temperature=0.0,
                    ),
                    AuditDecision,
                )
                supported = bool(
                    decision.question_subject_supported
                    and decision.all_required_links_supported
                    and decision.requested_value_supported
                )
                passed = supported is case.expected_supported
                passed_count += passed
                row: dict[str, object] = {
                    "case": case.name,
                    "question_subject_supported": decision.question_subject_supported,
                    "all_required_links_supported": decision.all_required_links_supported,
                    "requested_value_supported": decision.requested_value_supported,
                    "supported": supported,
                    "passed": passed,
                }
            except Exception as exc:
                row = {
                    "case": case.name,
                    "failure_category": (
                        "invalid_output"
                        if isinstance(exc, (StructuredOutputError, ValueError, TypeError))
                        else "unavailable"
                    ),
                    "supported": False,
                    "passed": False,
                }
            print(json.dumps(row, sort_keys=True), file=output)
    finally:
        provider_logger.disabled = was_disabled
    all_passed = passed_count == len(cases)
    print(
        json.dumps(
            {
                "case": "summary",
                "passed_count": passed_count,
                "case_count": len(cases),
                "passed": all_passed,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0 if all_passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run synthetic checks with the configured official model, not official evaluation."""
    if argv:
        print(json.dumps({"case": "preflight", "passed": False}), flush=True)
        return 2
    try:
        settings = Settings.from_env()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.selector_enabled
        ):
            raise ValueError("official selector required")
        settings.validate_runtime()
    except Exception:
        print(json.dumps({"case": "preflight", "passed": False}), flush=True)
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
        print(json.dumps({"case": "probe", "passed": False}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
