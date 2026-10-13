"""The selected-evidence audit probe reports only synthetic aggregate verdicts."""

import importlib
import json
from io import StringIO

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM


def test_audit_probe_requires_subject_path_and_value_for_every_case() -> None:
    probe = importlib.import_module("scripts.selected_evidence_audit_probe")
    llm = FakeStructuredLLM(
        [
            {
                "question_subject_supported": False,
                "all_required_links_supported": True,
                "requested_value_supported": True,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": True,
                "requested_value_supported": True,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": True,
                "requested_value_supported": True,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": True,
                "requested_value_supported": True,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": False,
                "requested_value_supported": True,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": True,
                "requested_value_supported": False,
            },
            {
                "question_subject_supported": True,
                "all_required_links_supported": True,
                "requested_value_supported": True,
            },
        ]
    )
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 0
    assert [row["case"] for row in rows] == [
        "wrong_subject_complete_chain",
        "connected_chain",
        "alias_chain",
        "direct_two_attributes",
        "disconnected_bridge",
        "missing_value",
        "paraphrased_direct",
        "summary",
    ]
    assert [row["supported"] for row in rows[:-1]] == [
        False, True, True, True, False, False, True
    ]
    assert all(row["passed"] is True for row in rows)
    assert rows[-1]["passed_count"] == 7
    assert len(llm.requests) == 7
    assert all(len(request.payload["selected_evidence"]) >= 1 for request in llm.requests)
    assert all(request.prompt_version == "subject-path-audit-probe-v1" for request in llm.requests)
    assert "Dossier" not in output.getvalue()
    assert "Relay" not in output.getvalue()
    assert "Compartment" not in output.getvalue()


def test_audit_probe_sanitizes_provider_failure() -> None:
    probe = importlib.import_module("scripts.selected_evidence_audit_probe")
    llm = FakeStructuredLLM([RuntimeError("private model response")])
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 1
    assert rows[0] == {
        "case": "wrong_subject_complete_chain",
        "failure_category": "unavailable",
        "passed": False,
        "supported": False,
    }
    assert "private model response" not in output.getvalue()
