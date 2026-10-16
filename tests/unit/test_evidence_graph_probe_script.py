"""The fixed paired evidence-graph suite must pass deterministically and leak nothing."""

import io
import json
from types import SimpleNamespace

import httpx
import pytest
import scripts.evidence_graph_probe as probe
from scripts.evidence_graph_probe import CASES, proposal_responses, run_probe

from masm.config import RuntimeProfile
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import OpenAICompatibleLLM
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


def test_identical_planner_inputs_are_distinguished_by_validator_provenance() -> None:
    by_planner_input: dict[tuple[object, ...], list[probe.Case]] = {}
    for case in CASES:
        key = (case.question, case.candidates, case.options)
        by_planner_input.setdefault(key, []).append(case)

    for twins in by_planner_input.values():
        if len(twins) < 2:
            continue
        validator_contexts = {
            (case.source_context, case.context_pairs)
            for case in twins
        }
        assert len(validator_contexts) == len(twins)


def test_scripted_suite_passes_every_positive_and_negative_case() -> None:
    code, rows = _run(FakeStructuredLLM(proposal_responses()))

    assert code == 0
    summary = rows[-1]
    assert summary["case"] == "summary"
    assert summary["case_count"] == len(CASES)
    assert summary["positive_passed"] == summary["positive_total"] == 13
    assert summary["negative_passed"] == summary["negative_total"] == 13
    assert summary["fallbacks"] == 0
    assert summary["failure_count"] == 0
    assert summary["failure_reasons"] == {"passed": 26}
    assert summary["state_transitions"] == {
        "insufficient->insufficient": 8,
        "partial->partial": 3,
        "sufficient->sufficient": 15,
    }
    assert summary["candidate_eligible"] is True
    for row in rows[:-1]:
        assert row["planner_schema_valid"] is True
        assert row["requirement_count"] >= 1
        assert row["support_count"] >= 0


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
            "planner_failure_reason",
            "selected_count",
            "selected_source_count",
            "planner_schema_valid",
            "requirement_count",
            "support_count",
            "link_count",
            "case_count",
            "positive_total",
            "positive_passed",
            "negative_total",
            "negative_passed",
            "fallbacks",
            "failure_count",
            "failure_reasons",
            "planner_failure_reasons",
            "state_transitions",
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
        position for position, case in enumerate(CASES) if case.name == "direct_fact_negative"
    )
    responses[index] = {
        "requirements": [{"question_quote": "Where did I store the amber key", "kind": "location"}],
        "supports": [
            {
                "requirement_index": 0,
                "candidate_index": 0,
                "candidate_quote": "The amber key matters to me.",
                "role": "direct",
            }
        ],
        "links": [],
    }
    code, rows = _run(FakeStructuredLLM(responses))

    assert code == 1
    summary = rows[-1]
    assert summary["positive_passed"] == summary["positive_total"] == 13
    assert summary["negative_passed"] == summary["negative_total"] - 1
    assert summary["failure_reasons"] == {"passed": 25, "state_mismatch": 1}
    assert summary["state_transitions"]["insufficient->sufficient"] == 1
    assert summary["candidate_eligible"] is False


def test_stability_gate_runs_all_five_rounds_before_accepting() -> None:
    responses = [
        response
        for _ in range(5)
        for response in proposal_responses()
    ]
    llm = FakeStructuredLLM(responses)
    output = io.StringIO()

    code = probe.run_stability_probe(llm, output)

    rows = _rows(output)
    round_results = [row for row in rows if row["case"] == "round_result"]
    assert code == 0
    assert len(llm.requests) == 5 * len(CASES)
    assert [row["round"] for row in round_results] == [1, 2, 3, 4, 5]
    assert all(row["passed"] is True for row in round_results)
    assert rows[-1] == {
        "case": "stability_summary",
        "round_count": 5,
        "rounds_passed": 5,
        "candidate_eligible": True,
    }


def test_stability_gate_rejects_one_failed_round_without_stopping_early() -> None:
    rounds = [proposal_responses() for _ in range(5)]
    broken_index = next(
        position
        for position, case in enumerate(CASES)
        if case.name == "direct_fact_negative"
    )
    rounds[2][broken_index] = {
        "requirements": [
            {"question_quote": "Where did I store the amber key", "kind": "location"}
        ],
        "supports": [
            {
                "requirement_index": 0,
                "candidate_index": 0,
                "candidate_quote": "The amber key matters to me.",
                "role": "direct",
            }
        ],
        "links": [],
    }
    llm = FakeStructuredLLM(
        [response for round_responses in rounds for response in round_responses]
    )
    output = io.StringIO()

    code = probe.run_stability_probe(llm, output)

    rows = _rows(output)
    round_results = [row for row in rows if row["case"] == "round_result"]
    assert code == 1
    assert len(llm.requests) == 5 * len(CASES)
    assert [row["passed"] for row in round_results] == [True, True, False, True, True]
    assert rows[-1] == {
        "case": "stability_summary",
        "round_count": 5,
        "rounds_passed": 4,
        "candidate_eligible": False,
    }


def test_stability_gate_output_never_contains_suite_content() -> None:
    llm = FakeStructuredLLM(
        [response for _ in range(5) for response in proposal_responses()]
    )
    output = io.StringIO()

    probe.run_stability_probe(llm, output)

    rendered = output.getvalue()
    for secret in ("amber key", "drawer three", "Zephyr", "Porto", "Nora", "琥珀", "工作坊"):
        assert secret not in rendered


def test_provider_check_reports_only_safe_http_status_metadata() -> None:
    private_body = "private-provider-body"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": private_body})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAICompatibleLLM(
        model="gpt-4o-mini",
        base_url="https://private-provider.invalid/v1",
        api_key="private-api-key",
        client=client,
        max_attempts=1,
    )
    output = io.StringIO()

    code = probe.run_provider_check(llm, output)

    assert code == 1
    assert calls == 1
    assert _rows(output) == [
        {
            "attempt": 1,
            "case": "provider_check",
            "category": "http_status",
            "status_code": 401,
        }
    ]
    rendered = output.getvalue()
    for secret in (private_body, "private-provider.invalid", "private-api-key"):
        assert secret not in rendered


def test_main_stability_flag_routes_to_the_fixed_five_round_gate(
    monkeypatch, capsys
) -> None:
    llm = FakeStructuredLLM(
        [response for _ in range(5) for response in proposal_responses()]
    )
    llm.close = lambda: None  # type: ignore[attr-defined]
    settings = SimpleNamespace(
        runtime_profile=RuntimeProfile.OFFICIAL_MASM,
        llm_model="fake-model",
        llm_base_url="https://provider.invalid/v1",
        llm_api_key="private-key",
        model_timeout_seconds=30.0,
        validate_runtime=lambda: None,
    )
    monkeypatch.setattr(
        probe.Settings, "from_env", classmethod(lambda cls: settings)
    )
    monkeypatch.setattr(probe, "OpenAICompatibleLLM", lambda **kwargs: llm)

    code = probe.main(["--stability"])

    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert len(llm.requests) == 5 * len(CASES)
    assert rows[-1] == {
        "case": "stability_summary",
        "round_count": 5,
        "rounds_passed": 5,
        "candidate_eligible": True,
    }


def test_main_provider_check_flag_routes_to_one_planner_call(
    monkeypatch, capsys
) -> None:
    llm = FakeStructuredLLM([proposal_responses()[0]])
    llm.close = lambda: None  # type: ignore[attr-defined]
    settings = SimpleNamespace(
        runtime_profile=RuntimeProfile.OFFICIAL_MASM,
        llm_model="fake-model",
        llm_base_url="https://provider.invalid/v1",
        llm_api_key="private-key",
        model_timeout_seconds=30.0,
        validate_runtime=lambda: None,
    )
    monkeypatch.setattr(
        probe.Settings, "from_env", classmethod(lambda cls: settings)
    )
    monkeypatch.setattr(probe, "OpenAICompatibleLLM", lambda **kwargs: llm)

    code = probe.main(["--provider-check"])

    assert code == 0
    assert len(llm.requests) == 1
    assert _rows(io.StringIO(capsys.readouterr().out)) == [
        {"case": "provider_check", "category": "success"}
    ]


def test_main_sanitizes_settings_parse_failure(monkeypatch, capsys) -> None:
    def raise_private_parse_error(cls) -> None:
        raise ValueError("private-malformed-value")

    monkeypatch.setattr(
        probe.Settings,
        "from_env",
        classmethod(raise_private_parse_error),
    )

    code = probe.main(["--stability"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == '{"case":"probe_error","passed":false}\n'
    assert captured.err == ""
    assert "private-malformed-value" not in captured.out


def test_main_stability_gate_rejects_debug_reverse_order(monkeypatch) -> None:
    settings = SimpleNamespace(runtime_profile=RuntimeProfile.LOCAL_FAKE)
    monkeypatch.setattr(
        probe.Settings, "from_env", classmethod(lambda cls: settings)
    )

    with pytest.raises(SystemExit) as exc_info:
        probe.main(["--stability", "--reverse"])

    assert exc_info.value.code == 2
