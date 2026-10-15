"""The fixed paired evidence-graph suite must pass deterministically and leak nothing."""

import io
import json

from scripts.evidence_graph_probe import CASES, proposal_responses, run_probe

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_graph import EvidenceGraphProposal


def _rows(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _run(llm: FakeStructuredLLM) -> tuple[int, list[dict]]:
    output = io.StringIO()
    code = run_probe(llm, output)
    return code, _rows(output)


def test_case_table_is_fixed_and_paired_with_valid_proposals() -> None:
    assert len(CASES) >= 20
    assert len({case.name for case in CASES}) == len(CASES)
    positive = [case for case in CASES if case.positive]
    negative = [case for case in CASES if not case.positive]
    assert len(positive) == len(negative)
    for case in CASES:
        EvidenceGraphProposal.model_validate(dict(case.proposal))
    for twin in negative:
        assert twin.name.endswith("_negative")
        assert any(case.name == twin.name.removesuffix("_negative") for case in positive)


def test_scripted_suite_passes_every_positive_and_negative_case() -> None:
    code, rows = _run(FakeStructuredLLM(proposal_responses()))

    assert code == 0
    summary = rows[-1]
    assert summary["case"] == "summary"
    assert summary["case_count"] == len(CASES)
    assert summary["positive_passed"] == summary["positive_total"] == 12
    assert summary["negative_passed"] == summary["negative_total"] == 12
    assert summary["fallbacks"] == 0
    assert summary["failure_count"] == 0
    assert summary["candidate_eligible"] is True


def test_suite_output_never_contains_question_or_candidate_text() -> None:
    output = io.StringIO()
    run_probe(FakeStructuredLLM(proposal_responses()), output)

    rendered = output.getvalue()
    for secret in ("amber key", "drawer three", "Zephyr", "Porto", "Nora", "琥珀", "工作坊"):
        assert secret not in rendered
    for row in _rows(output):
        assert set(row) <= {
            "case",
            "positive",
            "passed",
            "evidence_state",
            "abstained",
            "fallback",
            "failure_category",
            "selected_count",
            "selected_source_count",
            "link_count",
            "case_count",
            "positive_total",
            "positive_passed",
            "negative_total",
            "negative_passed",
            "fallbacks",
            "failure_count",
            "candidate_eligible",
        }


def test_empty_proposals_cannot_pass_any_case() -> None:
    empty = {"requirements": [], "supports": [], "links": []}
    code, rows = _run(FakeStructuredLLM([empty] * len(CASES)))

    assert code == 1
    summary = rows[-1]
    assert summary["positive_passed"] == 0
    assert summary["negative_passed"] == 0
    assert summary["fallbacks"] == len(CASES)
    assert summary["candidate_eligible"] is False


def test_provider_failures_never_count_as_passed_cases() -> None:
    code, rows = _run(FakeStructuredLLM([RuntimeError("private-provider-detail")] * len(CASES)))

    assert code == 1
    summary = rows[-1]
    assert summary["positive_passed"] == 0
    assert summary["negative_passed"] == 0
    assert summary["failure_count"] == len(CASES)
    assert summary["candidate_eligible"] is False
    assert "private-provider-detail" not in json.dumps(rows)


def test_one_broken_negative_twin_rejects_the_whole_run() -> None:
    responses = proposal_responses()
    index = next(
        position for position, case in enumerate(CASES) if case.name == "missing_value_negative"
    )
    responses[index] = {
        "requirements": [{"question_quote": "What is the storage code", "kind": "attribute"}],
        "supports": [
            {
                "requirement_index": 0,
                "candidate_index": 0,
                "candidate_quote": "I stored the amber key in drawer three.",
                "role": "direct",
            }
        ],
        "links": [],
    }
    code, rows = _run(FakeStructuredLLM(responses))

    assert code == 1
    summary = rows[-1]
    assert summary["positive_passed"] == summary["positive_total"] == 12
    assert summary["negative_passed"] == summary["negative_total"] - 1
    assert summary["candidate_eligible"] is False
