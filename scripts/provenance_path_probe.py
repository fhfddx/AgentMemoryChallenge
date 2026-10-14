"""Diagnostic-only quote-backed relation path protocol; not used by Search."""

import json
import logging
import re
import sys
from collections.abc import Sequence
from typing import TextIO

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from scripts.selected_evidence_audit_probe import _Case, _cases

from masm.config import RuntimeProfile, Settings
from masm.providers.llm import (
    ModelRequest,
    OpenAICompatibleLLM,
    StructuredLLM,
    StructuredOutputError,
)

MAX_CLAIMS = 16
_FIRST_PERSON_REFERENCE = re.compile(
    r"(?<!\w)(?:I|me|my|mine|we|us|our|ours)(?!\w)", re.IGNORECASE
)
_EXTRACT_VERSION = "provenance-path-extract-probe-v2"
_JUDGE_VERSION = "provenance-path-judge-probe-v2"
_EXTRACT_PROMPT = (
    "Read only the selected evidence, without a question. Extract up to 16 directly "
    "stated atomic facts as quote-backed claims. Return JSON with claims only. For each "
    "claim, source_index is the index of its own source and quote must be an exact "
    "contiguous substring copied from that source. Do not rewrite, normalize, label, or "
    "summarize any part of a quote. Prefer the shortest complete sentence or phrase that "
    "retains the stated subject, relation, and object. Do not invent an alias, connection, "
    "or unstated value. Keep explicit alias statements and unknown-value statements as "
    "verbatim quote claims, but do not fill in missing facts."
)
_JUDGE_PROMPT = (
    "Judge whether these validated, quote-backed claims fully support the question. "
    "Return JSON with question_subject_supported, all_required_links_supported, "
    "requested_value_supported, and used_claim_indices. Use only the claims; do not "
    "answer the question. Interpret each exact quote as a claim and trace a complete path "
    "from the questioned subject to each requested value. An explicit alias claim may "
    "connect names. has_first_person_reference is derived locally from its exact quote; "
    "a first-person quote refers to that source's speaker and may satisfy a question about "
    "the speaker without identical wording. Do not equate speakers from different sources "
    "without evidence. A complete path about a different subject is "
    "unsupported, even if it reaches a plausible value. Likewise, shared topic, type, "
    "location or route is not a bridge. An unknown or undocumented value does not support "
    "a requested concrete value. When all requested attributes and every needed link "
    "are supported, list the claim indices used. Otherwise return false for the missing "
    "conditions and used_claim_indices=[]. Never require a verbatim identifier from the "
    "question to occur in a quote; judge aliases and paraphrases by their stated meaning."
)
_PARAPHRASED_MULTI_SOURCE = _Case(
    "paraphrased_multi_source",
    "Which city hosts the workshop the speaker signed up for?",
    (
        "I signed up for the Zephyr workshop.",
        "The Zephyr workshop is held in Lisbon.",
    ),
    True,
)


class Claim(BaseModel):
    """One atomic fact copied verbatim from a single source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_index: StrictInt
    quote: str = Field(min_length=1, max_length=1000)


class ClaimExtraction(BaseModel):
    """Question-blind extraction from the selected evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claims: tuple[Claim, ...]


class PathJudgement(BaseModel):
    """Independent judgment using only validated quote-backed claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_subject_supported: StrictBool
    all_required_links_supported: StrictBool
    requested_value_supported: StrictBool
    used_claim_indices: tuple[StrictInt, ...]


class DiagnosticValidationError(ValueError):
    """Content-safe local validation failure with a fixed reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_claims(extraction: ClaimExtraction, sources: Sequence[str]) -> tuple[Claim, ...]:
    """Reject invented source references or non-verbatim quotes."""
    claims = extraction.claims
    if not 1 <= len(claims) <= MAX_CLAIMS:
        raise DiagnosticValidationError("invalid_claim_count")
    for claim in claims:
        if not claim.quote.strip():
            raise DiagnosticValidationError("blank_quote")
        if not 0 <= claim.source_index < len(sources):
            raise DiagnosticValidationError("source_index_out_of_range")
        if claim.quote not in sources[claim.source_index]:
            raise DiagnosticValidationError("quote_not_in_source")
    return claims


def judgement_supported(decision: PathJudgement, claim_count: int) -> bool:
    """Require complete flags and a nonempty, usable claim-index path."""
    indices = decision.used_claim_indices
    return bool(
        decision.question_subject_supported
        and decision.all_required_links_supported
        and decision.requested_value_supported
        and indices
        and len(indices) <= claim_count
        and len(indices) == len(set(indices))
        and all(0 <= index < claim_count for index in indices)
    )


def _judge_claim_payload(claim: Claim) -> dict[str, object]:
    """Add only a deterministic marker derived from an already validated quote."""
    return {
        **claim.model_dump(mode="json"),
        "has_first_person_reference": bool(_FIRST_PERSON_REFERENCE.search(claim.quote)),
    }


def run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int:
    """Run synthetic cases, printing only aggregate decisions and coarse failures."""
    cases = (*_cases(), _PARAPHRASED_MULTI_SOURCE)
    passed_count = 0
    provider_logger = logging.getLogger("masm.provider")
    was_disabled = provider_logger.disabled
    provider_logger.disabled = True
    try:
        for case in cases:
            failure_stage = "extraction"
            try:
                extraction = llm.complete_json(
                    ModelRequest(
                        prompt=_EXTRACT_PROMPT,
                        payload={
                            "selected_evidence": [
                                {"index": index, "text": source}
                                for index, source in enumerate(case.selected_evidence)
                            ]
                        },
                        model=llm.model,
                        prompt_version=_EXTRACT_VERSION,
                        timeout_seconds=settings.selector_timeout_seconds,
                        max_attempts=1,
                        temperature=0.0,
                    ),
                    ClaimExtraction,
                )
                failure_stage = "validation"
                claims = validate_claims(extraction, case.selected_evidence)
                failure_stage = "judgment"
                decision = llm.complete_json(
                    ModelRequest(
                        prompt=_JUDGE_PROMPT,
                        payload={
                            "question": case.question,
                            "claims": [_judge_claim_payload(claim) for claim in claims],
                        },
                        model=llm.model,
                        prompt_version=_JUDGE_VERSION,
                        timeout_seconds=settings.selector_timeout_seconds,
                        max_attempts=1,
                        temperature=0.0,
                    ),
                    PathJudgement,
                )
                indices = decision.used_claim_indices
                if indices and (
                    len(indices) != len(set(indices))
                    or any(not 0 <= index < len(claims) for index in indices)
                ):
                    raise DiagnosticValidationError("invalid_claim_path")
                if (
                    decision.question_subject_supported
                    and decision.all_required_links_supported
                    and decision.requested_value_supported
                    and not indices
                ):
                    raise DiagnosticValidationError("supported_path_without_indices")
                supported = judgement_supported(decision, len(claims))
                passed = supported is case.expected_supported
                passed_count += passed
                row: dict[str, object] = {
                    "case": case.name,
                    "claim_count": len(claims),
                    "source_count": len({claim.source_index for claim in claims}),
                    "used_claim_count": len(indices),
                    "supported": supported,
                    "passed": passed,
                    "failure_category": "none",
                }
            except Exception as exc:
                invalid_output = isinstance(
                    exc,
                    (
                        StructuredOutputError,
                        ValidationError,
                        DiagnosticValidationError,
                        ValueError,
                        TypeError,
                    ),
                )
                row = {
                    "case": case.name,
                    "supported": False,
                    "passed": False,
                    "failure_category": "invalid_output" if invalid_output else "unavailable",
                    "failure_stage": failure_stage,
                    "failure_reason": (
                        exc.reason
                        if isinstance(exc, DiagnosticValidationError)
                        else "schema"
                        if invalid_output
                        else "provider"
                    ),
                }
            print(json.dumps(row, sort_keys=True), file=output)
    finally:
        provider_logger.disabled = was_disabled
    passed = passed_count == len(cases)
    print(
        json.dumps(
            {
                "case": "summary",
                "case_count": len(cases),
                "passed_count": passed_count,
                "passed": passed,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0 if passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Use the existing official model configuration, without database access."""
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
