"""Quote-backed path diagnostics must reject invented provenance."""

import json
from io import StringIO

import pytest
from scripts.provenance_path_probe import (
    Claim,
    ClaimExtraction,
    PathJudgement,
    judgement_supported,
    run_probe,
    validate_claims,
)

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM

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
        validate_claims(ClaimExtraction(claims=(_claim(1, subject_is_speaker=True),)), _SOURCES)


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


def _fake_claim(
    source_index: int,
    quote: str,
    subject: str,
    relation: str,
    value: str,
    *,
    speaker: bool = False,
) -> dict[str, object]:
    return {
        "source_index": source_index,
        "quote": quote,
        "subject_mention": subject,
        "relation_mention": relation,
        "object_mention": value,
        "subject_is_speaker": speaker,
    }


def test_probe_separates_source_claim_extraction_from_question_judgment() -> None:
    dossier = [
        _fake_claim(
            0,
            "Records for Dossier-A are assigned to the transfer phrase Relay-Cobalt.",
            "Dossier-A",
            "assigned to",
            "Relay-Cobalt",
        ),
        _fake_claim(
            1,
            "Every record routed by Relay-Cobalt ultimately arrives at Compartment Ember.",
            "Relay-Cobalt",
            "arrives at",
            "Compartment Ember",
        ),
    ]
    cases = [
        (dossier, False),
        (dossier, True),
        (
            [
                _fake_claim(
                    0,
                    "The Sapphire file is also recorded as Dossier-A.",
                    "Sapphire file",
                    "also recorded as",
                    "Dossier-A",
                ),
                _fake_claim(
                    1, str(dossier[0]["quote"]), "Dossier-A", "assigned to", "Relay-Cobalt"
                ),
                _fake_claim(
                    2, str(dossier[1]["quote"]), "Relay-Cobalt", "arrives at", "Compartment Ember"
                ),
            ],
            True,
        ),
        (
            [
                _fake_claim(
                    0,
                    "The Atlas project uses code name Zephyr.",
                    "Atlas project",
                    "uses code name",
                    "Zephyr",
                ),
                _fake_claim(
                    1,
                    "The Atlas project review is scheduled on April 18.",
                    "Atlas project",
                    "scheduled on",
                    "April 18",
                ),
            ],
            True,
        ),
        (
            [
                _fake_claim(
                    0,
                    "Nora plans to attend the Zephyr workshop.",
                    "Nora",
                    "plans to attend",
                    "Zephyr workshop",
                ),
                _fake_claim(
                    1,
                    "The Aurora workshop will be held in Lisbon.",
                    "Aurora workshop",
                    "held in",
                    "Lisbon",
                ),
            ],
            False,
        ),
        (
            [
                _fake_claim(
                    0,
                    "The Isolated dossier's destination has not been documented.",
                    "Isolated dossier",
                    "destination has",
                    "not been documented",
                ),
            ],
            False,
        ),
        (
            [
                _fake_claim(
                    0,
                    "I placed the cabinet key in drawer three.",
                    "I",
                    "placed",
                    "drawer three",
                    speaker=True,
                ),
            ],
            True,
        ),
        (
            [
                _fake_claim(
                    0,
                    "I signed up for the Zephyr workshop.",
                    "I",
                    "signed up for",
                    "Zephyr workshop",
                    speaker=True,
                ),
                _fake_claim(
                    1,
                    "The Zephyr workshop is held in Lisbon.",
                    "Zephyr workshop",
                    "held in",
                    "Lisbon",
                ),
            ],
            True,
        ),
    ]
    responses: list[dict[str, object]] = []
    for claims, supported in cases:
        responses.extend(
            [
                {"claims": claims},
                {
                    "question_subject_supported": supported,
                    "all_required_links_supported": supported,
                    "requested_value_supported": supported,
                    "used_claim_indices": list(range(len(claims))) if supported else [],
                },
            ]
        )
    llm = FakeStructuredLLM(responses)
    output = StringIO()

    result = run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result == 0
    assert [row["case"] for row in rows] == [
        "wrong_subject_complete_chain",
        "connected_chain",
        "alias_chain",
        "direct_two_attributes",
        "disconnected_bridge",
        "missing_value",
        "paraphrased_direct",
        "paraphrased_multi_source",
        "summary",
    ]
    assert rows[-1] == {"case": "summary", "case_count": 8, "passed": True, "passed_count": 8}
    assert len(llm.requests) == 16
    for extraction, judgment in zip(llm.requests[::2], llm.requests[1::2], strict=True):
        assert "question" not in extraction.payload
        assert "selected_evidence" in extraction.payload
        assert "question" in judgment.payload
        assert "claims" in judgment.payload
        assert "selected_evidence" not in judgment.payload
    for forbidden in ("Dossier-A", "Sapphire file", "Zephyr", "Lisbon", "I placed"):
        assert forbidden not in output.getvalue()


def test_probe_sanitizes_invalid_quote_and_provider_error() -> None:
    invalid = FakeStructuredLLM(
        [
            {
                "claims": [
                    _fake_claim(0, "Private fabricated quote", "Private", "fabricated", "quote")
                ]
            },
            RuntimeError("private model response"),
        ]
    )
    output = StringIO()

    result = run_probe(invalid, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result == 1
    assert rows[0]["failure_category"] == "invalid_output"
    assert rows[1]["failure_category"] == "unavailable"
    assert "Private" not in output.getvalue()
    assert "private model response" not in output.getvalue()
