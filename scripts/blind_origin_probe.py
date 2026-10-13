"""Probe question-blind evidence-subject extraction without changing Search."""

import json
import logging
import sys
from collections.abc import Sequence
from typing import TextIO

from pydantic import BaseModel, ConfigDict, StrictBool
from scripts.selected_evidence_audit_probe import _cases

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import (
    ModelRequest,
    OpenAICompatibleLLM,
    StructuredLLM,
    StructuredOutputError,
)

_EXTRACT_PROMPT = (
    "Read only the selected evidence. Identify the starting person, object, or record "
    "whose attributes or outgoing relation path the evidence describes. Return JSON with "
    "origin_labels only. Include each explicit alias for that same starting subject. "
    "Exclude intermediate relays, destinations, other unrelated subjects, and generic "
    "topics. Do not infer a label that is not established in the evidence. You have no "
    "question; derive the starting subject only from the evidence."
)
_COMPARE_PROMPT = (
    "Decide whether the subject asked about in the question denotes one of the origin "
    "subjects listed from the evidence. Return JSON with matches only. Compare by meaning, "
    "explicit alias, and conversational context; never require identical wording or a "
    "verbatim identifier. Do not treat two differently named records as identical merely "
    "because they share a type, route, topic, place, or destination. The origin labels "
    "were extracted without seeing the question. If their identity with the question "
    "subject is not established, return false. Do not answer the question."
)


class OriginLabels(BaseModel):
    """Candidate origin names, extracted without access to the question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_labels: tuple[str, ...]


class OriginMatch(BaseModel):
    """Independent semantic identity decision using question and origin names only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matches: StrictBool


_CASE_NAMES = frozenset(
    {
        "wrong_subject_complete_chain",
        "connected_chain",
        "alias_chain",
        "direct_two_attributes",
        "paraphrased_direct",
    }
)


def run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int:
    """Report case-level booleans; never emit evidence or extracted names."""
    cases = tuple(case for case in _cases() if case.name in _CASE_NAMES)
    passed_count = 0
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for case in cases:
            try:
                origins = llm.complete_json(
                    ModelRequest(
                        prompt=_EXTRACT_PROMPT,
                        payload={
                            "selected_evidence": [
                                {"index": index, "text": content}
                                for index, content in enumerate(case.selected_evidence)
                            ]
                        },
                        model=llm.model,
                        prompt_version="blind-origin-extract-probe-v1",
                        timeout_seconds=settings.selector_timeout_seconds,
                        max_attempts=1,
                        temperature=0.0,
                    ),
                    OriginLabels,
                )
                if not origins.origin_labels or any(
                    not label.strip() for label in origins.origin_labels
                ):
                    raise ValueError("empty origin")
                comparison = llm.complete_json(
                    ModelRequest(
                        prompt=_COMPARE_PROMPT,
                        payload={
                            "question": case.question,
                            "origin_labels": list(origins.origin_labels),
                        },
                        model=llm.model,
                        prompt_version="blind-origin-compare-probe-v1",
                        timeout_seconds=settings.selector_timeout_seconds,
                        max_attempts=1,
                        temperature=0.0,
                    ),
                    OriginMatch,
                )
                passed = comparison.matches is case.expected_supported
                passed_count += passed
                row: dict[str, object] = {
                    "case": case.name,
                    "origin_count": len(origins.origin_labels),
                    "matches": comparison.matches,
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
                    "matches": False,
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
                "case_count": len(cases),
                "passed_count": passed_count,
                "passed": all_passed,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0 if all_passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run only synthetic model requests under official runtime configuration."""
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
