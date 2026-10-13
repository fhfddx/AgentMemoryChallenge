"""Quote-backed path diagnostics must reject invented provenance."""

import pytest
from scripts.provenance_path_probe import (
    Claim,
    ClaimExtraction,
    PathJudgement,
    judgement_supported,
    validate_claims,
)

_SOURCES = (
    "I signed up for the Zephyr workshop.",
    "The Zephyr workshop is held in Lisbon.",
)


def _claim(base_source_index: int = 0, **overrides: object) -> Claim:
    fields: dict[str, object] = {
        "source_index": base_source_index,
        "quote": _SOURCES[base_source_index],
        "subject_mention": "I" if base_source_index == 0 else "The Zephyr workshop",
        "relation_mention": "signed up for" if base_source_index == 0 else "is held in",
        "object_mention": "the Zephyr workshop" if base_source_index == 0 else "Lisbon",
        "subject_is_speaker": base_source_index == 0,
    }
    fields.update(overrides)
    return Claim(**fields)


def test_validates_quote_backed_claims_across_two_sources() -> None:
    claims = (_claim(0), _claim(1))

    assert validate_claims(ClaimExtraction(claims=claims), _SOURCES) == claims


@pytest.mark.parametrize(
    "claim",
    [
        _claim(0, source_index=1),
        _claim(0, subject_mention="Nora"),
        _claim(0, relation_mention="attended"),
        _claim(0, object_mention="Paris"),
        _claim(0, quote="I signed up for an imaginary workshop."),
    ],
)
def test_rejects_claims_not_grounded_in_their_own_quote(claim: Claim) -> None:
    with pytest.raises(ValueError):
        validate_claims(ClaimExtraction(claims=(claim,)), _SOURCES)


def test_rejects_empty_and_oversized_extractions() -> None:
    with pytest.raises(ValueError):
        validate_claims(ClaimExtraction(claims=()), _SOURCES)
    with pytest.raises(ValueError):
        validate_claims(ClaimExtraction(claims=(_claim(0),) * 17), _SOURCES)


def test_rejects_speaker_flag_without_first_person_subject() -> None:
    with pytest.raises(ValueError):
        validate_claims(
            ClaimExtraction(claims=(_claim(1, subject_is_speaker=True),)), _SOURCES
        )


@pytest.mark.parametrize("indices", [(), (0, 0), (0, 2), (-1,)])
def test_rejects_unsupported_or_invalid_judgment_paths(indices: tuple[int, ...]) -> None:
    decision = PathJudgement(
        question_subject_supported=True,
        all_required_links_supported=True,
        requested_value_supported=True,
        used_claim_indices=indices,
    )

    assert judgement_supported(decision, claim_count=2) is False


def test_accepts_complete_path_without_verbatim_question_subject() -> None:
    decision = PathJudgement(
        question_subject_supported=True,
        all_required_links_supported=True,
        requested_value_supported=True,
        used_claim_indices=(0, 1),
    )

    assert judgement_supported(decision, claim_count=2) is True
