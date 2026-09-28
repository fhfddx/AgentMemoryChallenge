"""汇总多个实验清单，不读取或输出评测原文。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.models import (
    ComparisonReport,
    ComparisonRow,
    ExperimentManifest,
    ExperimentMetrics,
)


def summarize_results(manifests: Sequence[ExperimentManifest]) -> ComparisonReport:
    rows = tuple(
        ComparisonRow(
            experiment_name=manifest.experiment_name,
            run_id=manifest.run_id,
            recall_at_10=manifest.metrics.recall_at_10,
            recall_at_100=manifest.metrics.recall_at_100,
            mrr=manifest.metrics.mrr,
            ndcg=manifest.metrics.ndcg,
            mean_latency_ms=manifest.metrics.mean_latency_ms,
            cost_usd=manifest.metrics.cost_usd,
            model_calls=manifest.metrics.model_calls,
            degradation_rate=manifest.metrics.degradation_rate,
        )
        for manifest in sorted(manifests, key=lambda value: value.experiment_name)
    )
    best_recall = max(rows, key=lambda row: row.recall_at_10).experiment_name if rows else None
    best_ndcg = max(rows, key=lambda row: row.ndcg).experiment_name if rows else None
    return ComparisonReport(rows=rows, best_recall_at_10=best_recall, best_ndcg=best_ndcg)


def _read_manifest(path: Path) -> ExperimentManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["metrics"] = ExperimentMetrics(**raw["metrics"])
    return ExperimentManifest(**raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 MASM 实验清单")
    parser.add_argument("manifests", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize_results([_read_manifest(path) for path in args.manifests])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"experiments": len(report.rows), "status": "ok"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
