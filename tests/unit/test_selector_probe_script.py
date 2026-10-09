"""The operator probe uses only synthetic evidence and safe aggregate output."""

import importlib
import json
import logging
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import httpx

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import OpenAICompatibleLLM


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
            "candidate_source_count": 2,
            "selected_count": 1,
            "selected_source_count": 1,
            "fallback": False,
            "abstained": False,
            "failure_category": "none",
            "passed": True,
        },
        {
            "case": "unrelated",
            "candidate_source_count": 2,
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
    assert json.loads(capsys.readouterr().out) == {
        "case": "preflight",
        "candidate_source_count": 0,
        "selected_count": 0,
        "selected_source_count": 0,
        "fallback": False,
        "abstained": False,
        "failure_category": "configuration",
        "passed": False,
    }


def test_main_sanitizes_invalid_environment_before_provider_construction(
    monkeypatch, capsys
) -> None:
    probe = importlib.import_module("scripts.selector_probe")
    monkeypatch.setenv("MASM_RUNTIME_PROFILE", "private-probe-key")

    exit_code = probe.main()

    output = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(output.out) == {
        "case": "preflight",
        "candidate_source_count": 0,
        "selected_count": 0,
        "selected_source_count": 0,
        "fallback": False,
        "abstained": False,
        "failure_category": "configuration",
        "passed": False,
    }
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
    assert json.loads(completed.stdout) == {
        "case": "preflight",
        "candidate_source_count": 0,
        "selected_count": 0,
        "selected_source_count": 0,
        "fallback": False,
        "abstained": False,
        "failure_category": "configuration",
        "passed": False,
    }
    assert completed.stderr == ""


def test_provider_failure_prints_only_probe_rows_not_provider_logs(capsys) -> None:
    probe = importlib.import_module("scripts.selector_probe")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-provider-secret", request=request)

    provider_logger = logging.getLogger("masm.provider")
    handler = logging.StreamHandler(sys.stderr)
    provider_logger.addHandler(handler)
    output = StringIO()
    try:
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            llm = OpenAICompatibleLLM(
                model="gpt-4o-mini",
                base_url="https://synthetic.example/v1",
                api_key="private-provider-secret",
                client=client,
            )
            exit_code = probe.run_probe(llm, Settings(database_url=""), output)
    finally:
        provider_logger.removeHandler(handler)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 1
    assert len(rows) == 2
    assert all(row["fallback"] and row["failure_category"] == "unavailable" for row in rows)
    assert capsys.readouterr().err == ""
