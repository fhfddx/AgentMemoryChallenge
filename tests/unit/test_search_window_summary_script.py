import json

from scripts.search_window_summary import summarize


def test_summary_ignores_payload_and_rejects_untrusted_enum_values():
    rows = [
        "private raw provider error",
        json.dumps({"event": "other", "query": "private query"}),
        json.dumps({"event": "search.completed", "selector_evidence_state": "partial",
                    "selector_abstained": True, "selector_fallback": False,
                    "selector_failure_category": "none", "candidate_count": 8,
                    "returned_count": 0, "selector_selected_count": 0}),
        json.dumps({"event": "search.completed", "selector_evidence_state": "secret-value",
                    "selector_failure_category": ["private-key"], "candidate_count": 0,
                    "returned_count": 0, "query": "private query"}),
    ]
    result = summarize(rows)
    assert result["search_rows"] == 2
    assert result["candidate_zero"] == 1
    assert result["returned_zero"] == 2
    assert result["evidence_states"] == {"partial": 1, "unknown": 1}
    assert result["failures"] == {"none": 1, "unknown": 1}
    assert result["non_json_lines"] == 1
    assert result["ignored_records"] == 1
    assert all(word not in json.dumps(result) for word in ("private", "secret", "query"))


def test_counts_packing_losses_without_assuming_question_identity():
    result = summarize([json.dumps({
        "event": "search.completed", "selector_selected_count": 3, "returned_count": 2,
    })])
    assert result["selected_more_than_returned"] == 1
    assert result["search_rows"] == 1
