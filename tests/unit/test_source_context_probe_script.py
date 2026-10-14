import io
import json

from scripts.source_context_probe import CASES, run_probe

from masm.providers.fakes import FakeStructuredLLM


def test_empty_selections_cannot_pass_positive_context_cases():
    llm = FakeStructuredLLM([
        {"evidence_state": "insufficient", "selected_indices": []}
    ] * 20)
    output = io.StringIO()
    assert run_probe(llm, output) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[-1]["candidate_eligible"] is False
    assert rows[-1]["candidate_passed"] < rows[-1]["case_count"]
    assert any(row.get("case") == "question_only" and row["passed"] for row in rows)
    assert "Which city" not in output.getvalue()


def test_provider_failures_do_not_count_as_successful_context_decisions():
    llm = FakeStructuredLLM([RuntimeError("private-provider-detail")] * 20)
    output = io.StringIO()
    assert run_probe(llm, output) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[-1]["candidate_eligible"] is False
    assert all(not row["passed"] for row in rows[:-1])
    assert "private-provider-detail" not in output.getvalue()


def test_eligibility_requires_complete_positive_coverage_and_no_false_positive():
    responses = []
    for case in CASES:
        responses.append({"evidence_state": "insufficient", "selected_indices": []})
        responses.append({
            "evidence_state": "sufficient" if case.required else "insufficient",
            "selected_indices": list(range(len(case.sources))) if case.required else [],
        })
    output = io.StringIO()
    assert run_probe(FakeStructuredLLM(responses), output) == 0
    summary = json.loads(output.getvalue().splitlines()[-1])
    assert summary["candidate_passed"] == 10
    assert summary["regressions"] == 0
    assert summary["candidate_eligible"] is True

    # Returning an assistant's unaccepted suggestion must fail the frozen gate.
    responses[7] = {"evidence_state": "sufficient", "selected_indices": [0]}
    output = io.StringIO()
    assert run_probe(FakeStructuredLLM(responses), output) == 1
    summary = json.loads(output.getvalue().splitlines()[-1])
    assert summary["regressions"] == 1
    assert summary["candidate_eligible"] is False
