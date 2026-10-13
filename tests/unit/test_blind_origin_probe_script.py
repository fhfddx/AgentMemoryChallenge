"""Question-blind subject extraction keeps the question out of its first call."""

import importlib
import json
from io import StringIO

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM


def test_blind_origin_probe_separates_extraction_from_question_comparison() -> None:
    probe = importlib.import_module("scripts.blind_origin_probe")
    llm = FakeStructuredLLM(
        [
            {"origin_labels": ["Dossier-A"]}, {"matches": False},
            {"origin_labels": ["Dossier-A"]}, {"matches": True},
            {"origin_labels": ["Dossier-A", "Sapphire file"]}, {"matches": True},
            {"origin_labels": ["Atlas project"]}, {"matches": True},
            {"origin_labels": ["I", "speaker"]}, {"matches": True},
            {"origin_labels": ["I", "speaker"]}, {"matches": True},
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
        "paraphrased_direct",
        "paraphrased_multi_source",
        "summary",
    ]
    assert [row["matches"] for row in rows[:-1]] == [
        False, True, True, True, True, True
    ]
    assert rows[-1]["passed_count"] == 6
    assert all(row["passed"] is True for row in rows)
    assert len(llm.requests) == 12
    for extraction, comparison in zip(llm.requests[::2], llm.requests[1::2], strict=True):
        assert "question" not in extraction.payload
        assert "selected_evidence" in extraction.payload
        assert "question" in comparison.payload
        assert "selected_evidence" not in comparison.payload
    assert "Dossier-A" not in output.getvalue()
    assert "Sapphire file" not in output.getvalue()


def test_blind_origin_probe_sanitizes_first_call_failure() -> None:
    probe = importlib.import_module("scripts.blind_origin_probe")
    llm = FakeStructuredLLM([RuntimeError("private model response")])
    output = StringIO()

    exit_code = probe.run_probe(llm, Settings(database_url=""), output)

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert exit_code == 1
    assert rows[0] == {
        "case": "wrong_subject_complete_chain",
        "failure_category": "unavailable",
        "matches": False,
        "passed": False,
    }
    assert "private model response" not in output.getvalue()
