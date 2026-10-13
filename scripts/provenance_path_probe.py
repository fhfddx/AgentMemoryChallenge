"""Diagnostic-only quote-backed relation path protocol; not used by Search."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

MAX_CLAIMS = 16


class Claim(BaseModel):
    """One atomic relation with mentions copied from a single source quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_index: StrictInt
    quote: str = Field(min_length=1, max_length=1000)
    subject_mention: str = Field(min_length=1, max_length=200)
    relation_mention: str = Field(min_length=1, max_length=200)
    object_mention: str = Field(min_length=1, max_length=200)
    subject_is_speaker: StrictBool


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


def validate_claims(
    extraction: ClaimExtraction, sources: Sequence[str]
) -> tuple[Claim, ...]:
    """Reject invented source references, quotes, or subject/relation/object mentions."""
    claims = extraction.claims
    if not 1 <= len(claims) <= MAX_CLAIMS:
        raise ValueError("invalid claim count")
    for claim in claims:
        if not 0 <= claim.source_index < len(sources):
            raise ValueError("source index out of range")
        if claim.quote not in sources[claim.source_index]:
            raise ValueError("quote not in source")
        if any(
            mention not in claim.quote
            for mention in (
                claim.subject_mention,
                claim.relation_mention,
                claim.object_mention,
            )
        ):
            raise ValueError("mention not in quote")
        if claim.subject_is_speaker and claim.subject_mention.casefold().strip() not in {
            "i", "me", "my", "mine", "we", "us", "our", "ours"
        }:
            raise ValueError("speaker flag lacks first-person subject")
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
