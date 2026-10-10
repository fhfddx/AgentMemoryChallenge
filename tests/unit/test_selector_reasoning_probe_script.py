"""The reasoning prompt probe stays synthetic and emits aggregate results only."""

import importlib
import json
from io import StringIO

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import StructuredOutputError


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
    assert exit_code == 1
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
        "production_eligible": False,
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
    assert exit_code == 1
    assert rows[0] == {
        "abstained": False,
        "candidate_count": 3,
        "case": "chain_complete",
        "failure_category": "invalid_output",
        "failure_detail": "out_of_range_indices",
        "evidence_state": "sufficient",
        "passed": False,
        "selected_count": 0,
        "selected_source_count": 0,
        "variant": "strict",
    }
    assert "99" not in output.getvalue()


def test_probe_reports_safe_invalid_output_subtypes() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0], "sufficient_evidence": False},
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [14, 15], "sufficient_evidence": True},
            StructuredOutputError("unsafe provider detail must stay hidden"),
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 1], "sufficient_evidence": True},
            {"selected_indices": [14, 15], "sufficient_evidence": True},
        ]
    )
    output = StringIO()

    probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["failure_detail"] == "insufficient_with_indices"
    assert rows[0]["evidence_state"] == "insufficient"
    assert rows[4]["failure_detail"] == "schema_validation"
    assert rows[4]["evidence_state"] == "unknown"
    assert "unsafe provider detail" not in output.getvalue()


def test_summary_reports_chain_as_no_longer_improving_on_the_production_prompt() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    all_cases_pass = [
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [], "evidence_state": "insufficient"},
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [14, 15], "evidence_state": "sufficient"},
    ]
    llm = FakeStructuredLLM([*all_cases_pass, *all_cases_pass])
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    summary = json.loads(output.getvalue().splitlines()[-1])
    assert exit_code == 0
    assert summary == {
        "case": "summary",
        "candidate_eligible": False,
        "chain_passed": 4,
        "production_eligible": True,
        "strict_passed": 4,
        "variant": "comparison",
    }


def test_probe_distinguishes_partial_from_invalid_missing_link_output() -> None:
    """三态探针必须把 partial 与 insufficient+indices 分开归类。

    `partial` 是合法输出（有直接事实但链路不完整），只是不满足该用例的期望；
    `insufficient+indices` 是跨字段矛盾，必须归入 invalid_output。
    """
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0], "evidence_state": "partial"},
            {"selected_indices": [], "evidence_state": "insufficient"},
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [14, 15], "evidence_state": "sufficient"},
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [0], "evidence_state": "insufficient"},
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [14, 15], "evidence_state": "sufficient"},
        ]
    )
    output = StringIO()

    probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["failure_category"] == "none"
    assert rows[0]["evidence_state"] == "partial"
    assert rows[0]["selected_count"] == 1
    assert rows[0]["abstained"] is False
    assert rows[0]["passed"] is False
    assert rows[1]["failure_category"] == "none"
    assert rows[1]["evidence_state"] == "insufficient"
    assert rows[1]["passed"] is True
    assert rows[5]["failure_detail"] == "insufficient_with_indices"
    assert rows[5]["failure_category"] == "invalid_output"
    assert rows[5]["passed"] is False
    assert "Nora" not in output.getvalue()


def test_probe_gates_the_production_prompt_independently_of_the_chain_variant() -> None:
    """生产 strict v7 全部通过时必须有明确的成功退出码，即使 chain 已不再更优。"""
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    strict_all_pass = [
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [], "evidence_state": "insufficient"},
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [14, 15], "evidence_state": "sufficient"},
    ]
    chain_one_fails = [
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [0], "evidence_state": "partial"},
        {"selected_indices": [0, 1], "evidence_state": "sufficient"},
        {"selected_indices": [14, 15], "evidence_state": "sufficient"},
    ]
    output = StringIO()

    exit_code = probe.run_probe(
        FakeStructuredLLM([*strict_all_pass, *chain_one_fails]),
        Settings(database_url=""),
        output,
    )

    assert exit_code == 0
    assert json.loads(output.getvalue().splitlines()[-1]) == {
        "case": "summary",
        "candidate_eligible": False,
        "chain_passed": 3,
        "production_eligible": True,
        "strict_passed": 4,
        "variant": "comparison",
    }


def test_strict_only_runs_only_the_production_prompt() -> None:
    """--strict-only 只跑生产 prompt：4 次调用，无 chain 行，生产门槛决定退出码。"""
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [], "evidence_state": "insufficient"},
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [14, 15], "evidence_state": "sufficient"},
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output, strict_only=True)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 0
    assert [row["variant"] for row in rows[:-1]] == ["strict"] * 4
    assert len(llm.requests) == 4
    assert {request.prompt_version for request in llm.requests} == {"reasoning-probe-strict-v1"}
    assert rows[-1] == {
        "case": "summary",
        "candidate_eligible": False,
        "chain_passed": 0,
        "production_eligible": True,
        "strict_passed": 4,
        "variant": "comparison",
    }


def test_strict_only_still_fails_the_gate_when_the_production_prompt_misses_a_case() -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0], "evidence_state": "partial"},
            {"selected_indices": [], "evidence_state": "insufficient"},
            {"selected_indices": [0, 1], "evidence_state": "sufficient"},
            {"selected_indices": [14, 15], "evidence_state": "sufficient"},
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output, strict_only=True)

    assert exit_code == 1
    assert json.loads(output.getvalue().splitlines()[-1]) == {
        "case": "summary",
        "candidate_eligible": False,
        "chain_passed": 0,
        "production_eligible": False,
        "strict_passed": 3,
        "variant": "comparison",
    }


def test_main_passes_strict_only_through_to_the_probe(monkeypatch) -> None:
    probe = importlib.import_module("scripts.selector_reasoning_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "official-masm")
    monkeypatch.setenv("MASM_EVIDENCE_SELECTOR_ENABLED", "1")
    monkeypatch.setenv("MASM_LLM_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setenv("MASM_LLM_API_KEY", "private-reasoning-probe-key")
    monkeypatch.setenv("MASM_EMBEDDING_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setenv("MASM_EMBEDDING_API_KEY", "private-embedding-key")
    llm = FakeStructuredLLM()
    monkeypatch.setattr(llm, "close", lambda: None, raising=False)
    seen: dict[str, object] = {}

    def fake_run(llm_, settings_, output_, *, strict_only=False):
        seen["strict_only"] = strict_only
        return 0

    monkeypatch.setattr(probe, "OpenAICompatibleLLM", lambda **kwargs: llm, raising=False)
    monkeypatch.setattr(probe, "run_probe", fake_run, raising=False)

    assert probe.main(["--strict-only"]) == 0
    assert seen["strict_only"] is True
    assert probe.main() == 0
    assert seen["strict_only"] is False


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
        "evidence_state": "unknown",
        "production_eligible": False,
        "passed": False,
        "variant": "comparison",
    }
    assert "private-reasoning-probe-key" not in output.out + output.err
