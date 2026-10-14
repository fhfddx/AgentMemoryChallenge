import io
import json

from scripts.completeness_repair_probe import CASES, run_probe

from masm.providers.fakes import FakeStructuredLLM


def _initial_response(case_name: str, *, cross_source_complete: bool = False):
    case = next(case for case in CASES if case.name == case_name)
    if not case.required:
        return {"evidence_state": "insufficient", "selected_indices": []}
    if case_name == "cross_source_context" and not cross_source_complete:
        return {"evidence_state": "sufficient", "selected_indices": [0]}
    return {
        "evidence_state": "sufficient",
        "selected_indices": list(range(len(case.sources))),
    }


def _responses_for_target_repair(*, audit_complete: bool = False):
    responses = []
    for case in CASES:
        responses.append(_initial_response(case.name))
        if case.name == "cross_source_context":
            responses.append({"evidence_complete": audit_complete})
            if not audit_complete:
                responses.append(
                    {"evidence_state": "sufficient", "selected_indices": [0, 1]}
                )
    return responses


def test_repair_candidate_must_complete_target_without_control_regressions():
    llm = FakeStructuredLLM(_responses_for_target_repair())
    output = io.StringIO()

    assert run_probe(llm, output) == 0

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    summary = rows[-1]
    assert summary == {
        "audit_only_passed": 9,
        "candidate_eligible": True,
        "case": "summary",
        "case_count": 10,
        "current_passed": 9,
        "failure_count": 0,
        "regressions": 0,
        "repair_passed": 10,
        "target_repaired": True,
    }
    target = [row for row in rows if row.get("case") == "cross_source_context"]
    assert [row["variant"] for row in target] == ["current", "audit_only", "repair"]
    assert target[-1]["selected_source_count"] == 2
    assert [request.prompt_version for request in llm.requests].count(
        "completeness-audit-v1"
    ) == 1
    assert [request.prompt_version for request in llm.requests].count(
        "completeness-repair-v1"
    ) == 1


def test_gate_rejects_audit_that_accepts_incomplete_single_source():
    output = io.StringIO()

    assert run_probe(
        FakeStructuredLLM(_responses_for_target_repair(audit_complete=True)), output
    ) == 1

    summary = json.loads(output.getvalue().splitlines()[-1])
    assert summary["repair_passed"] == 9
    assert summary["target_repaired"] is False
    assert summary["candidate_eligible"] is False


def test_audit_provider_failure_is_sanitized_and_fails_the_gate():
    responses = []
    for case in CASES:
        responses.append(_initial_response(case.name))
        if case.name == "cross_source_context":
            responses.append(RuntimeError("private-provider-detail"))
    output = io.StringIO()

    assert run_probe(FakeStructuredLLM(responses), output) == 1

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[-1]["failure_count"] == 1
    assert rows[-1]["candidate_eligible"] is False
    assert "private-provider-detail" not in output.getvalue()
