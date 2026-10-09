from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import compare_search_diagnostics


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_cli_reports_safe_aggregate_changes_without_sensitive_fields(tmp_path: Path) -> None:
    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    common = {
        "candidate_count": 4,
        "dedup_count": 2,
        "selector_candidate_count": 2,
        "selector_source_count": 2,
        "selector_fallback": False,
        "selector_failure_category": "none",
        "query": "private evaluation question",
        "content": "private answer material",
    }
    _write_jsonl(
        old_path,
        [
            {
                **common,
                "returned_count": 1,
                "selector_selected_count": 1,
                "selector_selected_source_count": 1,
                "selector_abstained": False,
            },
            {
                **common,
                "returned_count": 0,
                "selector_selected_count": 0,
                "selector_selected_source_count": 0,
                "selector_abstained": True,
            },
        ],
    )
    _write_jsonl(
        new_path,
        [
            {
                **common,
                "returned_count": 1,
                "selector_selected_count": 1,
                "selector_selected_source_count": 1,
                "selector_abstained": False,
            },
            {
                **common,
                "returned_count": 2,
                "selector_selected_count": 2,
                "selector_selected_source_count": 2,
                "selector_abstained": False,
            },
        ],
    )

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "compare_search_diagnostics.py"),
            str(old_path),
            str(new_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "v1 ROWS 2" in result.stdout
    assert "v2 ROWS 2" in result.stdout
    assert "CHANGED_ROWS [2]" in result.stdout
    assert "returned_zero=1" in result.stdout
    assert "returned_zero=0" in result.stdout
    assert "private evaluation question" not in result.stdout
    assert "private answer material" not in result.stdout


def test_cli_continues_when_one_diagnostics_file_is_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(
        new_path,
        [
            {
                "candidate_count": 0,
                "dedup_count": 0,
                "returned_count": 0,
                "selector_candidate_count": 0,
                "selector_selected_count": 0,
                "selector_source_count": 0,
                "selector_selected_source_count": 0,
                "selector_abstained": False,
                "selector_fallback": False,
                "selector_failure_category": "none",
            }
        ],
    )

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "compare_search_diagnostics.py"),
            str(missing_path),
            str(new_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"v1 MISSING {missing_path}" in result.stdout
    assert "v2 ROWS 1" in result.stdout
    assert "CHANGED_ROWS" not in result.stdout


def test_cli_defaults_to_server_smoke_diagnostics_paths() -> None:
    args = compare_search_diagnostics.parse_args([])

    assert args.old_path == Path("/tmp/smoke-search-diagnostics.jsonl")
    assert args.new_path == Path("/tmp/smoke-v2-search-diagnostics.jsonl")
