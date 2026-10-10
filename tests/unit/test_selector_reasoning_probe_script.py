"""The reasoning prompt probe stays synthetic and emits aggregate results only."""

import importlib
import json
from io import StringIO

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM


def test_probe_compares_strict_and_chain_prompts_without_printing_content() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [14, 15], "sufficient_evidence": True},
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 0
    assert [(row["variant"], row["case"]) for row in rows[:-1]] == [
        ("strict", "chain_complete"),
        ("strict", "chain_missing_link"),
        ("strict", "direct_multi_source"),
        ("strict", "crowded_chain"),
        ("chain", "chain_complete"),
        ("chain", "chain_missing_link"),
        ("chain", "direct_multi_source"),
        ("chain", "crowded_chain"),
    ]
    assert [row["passed"] for row in rows[:-1]] == [
        False,
        True,
        True,
        False,
        True,
        True,
        True,
        True,
    ]
    assert rows[-1] == {
        "case": "summary",
        "candidate_eligible": True,
        "chain_passed": 4,
        "strict_passed": 2,
        "variant": "comparison",
    }
    assert [len(request.payload["candidates"]) for request in llm.requests] == [
        3,
        3,
        2,
        16,
        3,
        3,
        2,
        16,
    ]
    assert {request.prompt_version for request in llm.requests} == {
        "reasoning-probe-strict-v1",
        "reasoning-probe-chain-v1",
    }
    assert "Nora" not in output.getvalue()
    assert "Lisbon" not in output.getvalue()


def test_probe_rejects_out_of_range_indices_as_sanitized_invalid_output() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [99], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [14, 15], "sufficient_evidence": True},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [14, 15], "sufficient_evidence": True},
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 0
    assert rows[0] == {
        "abstained": False,
        "candidate_count": 3,
        "case": "chain_complete",
        "failure_category": "invalid_output",
        "passed": False,
        "selected_count": 0,
        "selected_source_count": 0,
        "variant": "strict",
    }
    assert "99" not in output.getvalue()


def test_probe_requires_chain_prompt_to_improve_on_strict_prompt() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    all_cases_pass = [
        {"selected_indices": [0, 1], "sufficient_evidence": True},
        {"selected_indices": [], "sufficient_evidence": False},
        {"selected_indices": [0, 1], "sufficient_evidence": True},
        {"selected_indices": [14, 15], "sufficient_evidence": True},
    ]
    llm = FakeStructuredLLM([*all_cases_pass, *all_cases_pass])
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    summary = json.loads(output.getvalue().splitlines()[-1])
    assert exit_code == 1
    assert summary == {
        "case": "summary",
        "candidate_eligible": False,
        "chain_passed": 4,
        "strict_passed": 4,
        "variant": "comparison",
    }


def test_main_rejects_disabled_selector_without_printing_credentials(
    monkeypatch, capsys
) -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "official-masm")
    monkeypatch.setenv("MASM_EVIDENCE_SELECTOR_ENABLED", "0")
    monkeypatch.setenv("MASM_LLM_API_KEY", "private-reasoning-probe-key")

    exit_code = probe.main()

    output = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(output.out) == {
        "case": "preflight",
        "failure_category": "configuration",
        "passed": False,
        "variant": "comparison",
    }
    assert "private-reasoning-probe-key" not in output.out + output.err
