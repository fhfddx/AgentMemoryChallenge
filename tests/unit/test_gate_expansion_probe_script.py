import io
import json
from dataclasses import replace

from scripts.gate_expansion_probe import CASES, run_probe

from masm.providers.fakes import FakeStructuredLLM


def _passing_selector_responses():
    responses = []
    for case in CASES:
        if not case.expected_indices:
            responses.append({"evidence_state": "insufficient", "selected_indices": []})
        elif case.name in {"cross_source_chain", "crowded_cross_source_chain"}:
            responses.append(
                {
                    "evidence_state": "sufficient",
                    "selected_indices": [max(case.expected_indices)],
                }
            )
        else:
            responses.append(
                {
                    "evidence_state": "sufficient",
                    "selected_indices": sorted(case.expected_indices),
                }
            )
    return responses


def test_strong_anchor_expansion_repairs_both_cross_source_targets():
    llm = FakeStructuredLLM(_passing_selector_responses())
    output = io.StringIO()

    assert run_probe(llm, output) == 0

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    summary = rows[-1]
    assert summary == {
        "candidate_eligible": True,
        "case": "summary",
        "case_count": 8,
        "current_passed": 6,
        "eligible_variants": ["next_distinct", "top2_distinct"],
        "failure_count": 0,
        "next_distinct_passed": 8,
        "next_distinct_regressions": 0,
        "top2_distinct_passed": 8,
        "top2_distinct_regressions": 0,
    }
    targets = [
        row
        for row in rows
        if row.get("case") in {"cross_source_chain", "crowded_cross_source_chain"}
        and row.get("variant") != "current"
    ]
    assert len(targets) == 4
    assert all(row["passed"] and row["selected_source_count"] == 2 for row in targets)
    assert len(llm.requests) == len(CASES)


def test_admitted_direct_distractor_disqualifies_both_expansion_variants():
    cases = tuple(
        replace(case, strong_indices=frozenset({0, 1}))
        if case.name == "direct_with_high_rank_distractor"
        else case
        for case in CASES
    )
    output = io.StringIO()

    assert run_probe(
        FakeStructuredLLM(_passing_selector_responses()), output, cases=cases
    ) == 1

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    summary = rows[-1]
    assert summary["eligible_variants"] == []
    assert summary["next_distinct_regressions"] == 1
    assert summary["top2_distinct_regressions"] == 1
    assert summary["candidate_eligible"] is False


def test_selector_failure_is_counted_once_and_sanitized():
    responses = _passing_selector_responses()
    responses[0] = RuntimeError("private-provider-detail")
    output = io.StringIO()

    assert run_probe(FakeStructuredLLM(responses), output) == 1

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[-1]["failure_count"] == 1
    assert rows[-1]["candidate_eligible"] is False
    assert "private-provider-detail" not in output.getvalue()
