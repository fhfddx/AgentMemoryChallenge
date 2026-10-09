"""The operator probe uses only synthetic evidence and safe aggregate output."""

import importlib
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM


def test_probe_reports_positive_selection_and_unrelated_abstention() -> None:
    probe = importlib.import_module("scripts.selector_probe")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    assert exit_code == 0
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {
            "case": "positive",
            "selected_count": 1,
            "selected_source_count": 1,
            "fallback": False,
            "abstained": False,
            "failure_category": "none",
            "passed": True,
        },
        {
            "case": "unrelated",
            "selected_count": 0,
            "selected_source_count": 0,
            "fallback": False,
            "abstained": True,
            "failure_category": "none",
            "passed": True,
        },
    ]


def test_main_rejects_disabled_selector_without_printing_credentials(
    monkeypatch, capsys
) -> None:
    probe = importlib.import_module("scripts.selector_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "official-masm")
    monkeypatch.setenv("MASM_EVIDENCE_SELECTOR_ENABLED", "0")
    monkeypatch.setenv("MASM_LLM_API_KEY", "private-probe-key")

    exit_code = probe.main()

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {"error": "selector_disabled"}


def test_main_sanitizes_invalid_environment_before_provider_construction(
    monkeypatch, capsys
) -> None:
    probe = importlib.import_module("scripts.selector_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "private-probe-key")

    exit_code = probe.main()

    output = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(output.out) == {"error": "probe_failed", "category": "ValueError"}
    assert "private-probe-key" not in output.out + output.err


def test_main_uses_existing_provider_config_and_prints_only_safe_counts(
    monkeypatch, capsys
) -> None:
    probe = importlib.import_module("scripts.selector_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "official-masm")
    monkeypatch.setenv("MASM_EVIDENCE_SELECTOR_ENABLED", "1")
    monkeypatch.setenv("MASM_LLM_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setenv("MASM_LLM_API_KEY", "private-probe-key")
    monkeypatch.setenv("MASM_EMBEDDING_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setenv("MASM_EMBEDDING_API_KEY", "private-embedding-key")
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [0], "sufficient_evidence": True},
            {"selected_indices": [], "sufficient_evidence": False},
        ]
    )

    def provider(**kwargs):
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["base_url"] == "https://synthetic.example/v1"
        assert kwargs["api_key"] == "private-probe-key"
        return llm

    monkeypatch.setattr(probe, "OpenAICompatibleLLM", provider, raising=False)

    exit_code = probe.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert [row["passed"] for row in map(json.loads, output.splitlines())] == [True, True]
    assert "private-probe-key" not in output
    assert "private-embedding-key" not in output
    assert "Nora" not in output


def test_script_entrypoint_exits_without_model_call_when_selector_disabled() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["MASM_RUNTIME_PROFILE"] = "official-masm"
    environment["MASM_EVIDENCE_SELECTOR_ENABLED"] = "0"
    environment["PYTHONPATH"] = str(root / "src")

    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "selector_probe.py")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {"error": "selector_disabled"}
    assert completed.stderr == ""
