"""Reduce Docker log stdin to fixed search counters; never echo input records."""

import json
import sys
from collections import Counter
from collections.abc import Iterable
from typing import Any


def summarize(lines: Iterable[str]) -> dict[str, Any]:
    states: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    decisions: Counter[tuple[str, bool, bool]] = Counter()
    rows = ignored = non_json = empty_candidates = empty_returns = packing_losses = 0
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            non_json += 1
            continue
        if not isinstance(row, dict) or row.get("event") != "search.completed":
            ignored += 1
            continue
        rows += 1
        raw_state = row.get("selector_evidence_state")
        state = raw_state if raw_state in (
            "sufficient", "partial", "insufficient", "unknown",
        ) else "unknown"
        raw_failure = row.get("selector_failure_category")
        failure = raw_failure if raw_failure in (
            "none", "unavailable", "invalid_output",
        ) else "unknown"
        states[state] += 1
        failures[failure] += 1
        decisions[(state, row.get("selector_abstained") is True,
                   row.get("selector_fallback") is True)] += 1
        candidates, returned, selected = (
            row.get(name) for name in (
                "candidate_count", "returned_count", "selector_selected_count",
            )
        )
        empty_candidates += type(candidates) is int and candidates == 0
        empty_returns += type(returned) is int and returned == 0
        packing_losses += (type(returned) is int and type(selected) is int
                           and 0 <= returned < selected)
    return {
        "case": "search_window_summary", "search_rows": rows,
        "ignored_records": ignored, "non_json_lines": non_json,
        "candidate_zero": empty_candidates, "returned_zero": empty_returns,
        "selected_more_than_returned": packing_losses,
        "evidence_states": dict(sorted(states.items())),
        "failures": dict(sorted(failures.items())),
        "decisions": [{"state": state, "abstained": abstained, "fallback": fallback,
                       "count": count}
                      for (state, abstained, fallback), count in sorted(decisions.items())],
    }


def main() -> int:
    result = summarize(sys.stdin)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["search_rows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
