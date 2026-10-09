from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SAFE_FIELDS = (
    "candidate_count",
    "dedup_count",
    "returned_count",
    "selector_candidate_count",
    "selector_selected_count",
    "selector_source_count",
    "selector_selected_source_count",
    "selector_abstained",
    "selector_fallback",
    "selector_failure_category",
)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _print_run(label: str, rows: list[dict[str, Any]]) -> None:
    print(f"{label} ROWS {len(rows)}")
    for number, row in enumerate(rows, 1):
        print(
            f"{label} {number:02d}",
            f"cand={row.get('candidate_count')}",
            f"dedup={row.get('dedup_count')}",
            f"ret={row.get('returned_count')}",
            f"sel_cand={row.get('selector_candidate_count')}",
            f"sel={row.get('selector_selected_count')}",
            f"src={row.get('selector_source_count')}",
            f"sel_src={row.get('selector_selected_source_count')}",
            f"abs={row.get('selector_abstained')}",
            f"fb={row.get('selector_fallback')}",
            f"fail={row.get('selector_failure_category')}",
        )
    returned_dist = dict(sorted(Counter(row.get("returned_count") for row in rows).items()))
    selected_sources_dist = dict(
        sorted(Counter(row.get("selector_selected_source_count") for row in rows).items())
    )
    failures = dict(Counter(row.get("selector_failure_category", "missing") for row in rows))
    print(
        f"{label} SUMMARY",
        f"candidate_zero={sum(row.get('candidate_count', 0) == 0 for row in rows)}",
        f"returned_zero={sum(row.get('returned_count', 0) == 0 for row in rows)}",
        f"abstained={sum(bool(row.get('selector_abstained')) for row in rows)}",
        f"fallback={sum(bool(row.get('selector_fallback')) for row in rows)}",
        f"returned_dist={returned_dist}",
        f"selected_sources_dist={selected_sources_dist}",
        f"failures={failures}",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare sanitized search diagnostics.")
    parser.add_argument(
        "old_path",
        nargs="?",
        type=Path,
        default=Path("/tmp/smoke-search-diagnostics.jsonl"),
    )
    parser.add_argument(
        "new_path",
        nargs="?",
        type=Path,
        default=Path("/tmp/smoke-v2-search-diagnostics.jsonl"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    runs: dict[str, list[dict[str, Any]]] = {}
    for label, path in (("v1", args.old_path), ("v2", args.new_path)):
        if not path.exists():
            print(f"{label} MISSING {path}")
            continue
        rows = _load_rows(path)
        runs[label] = rows
        _print_run(label, rows)

    if set(runs) == {"v1", "v2"}:
        changed_rows = [
            number
            for number, (old_row, new_row) in enumerate(
                zip(runs["v1"], runs["v2"], strict=False), 1
            )
            if tuple(old_row.get(field) for field in SAFE_FIELDS)
            != tuple(new_row.get(field) for field in SAFE_FIELDS)
        ]
        print(f"CHANGED_ROWS {changed_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
