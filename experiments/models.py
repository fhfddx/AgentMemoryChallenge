"""实验数据、指标与清单模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BenchmarkItem:
    id: str
    user_id: str
    session_id: str
    messages: tuple[dict[str, Any], ...]
    match_token: str


@dataclass(frozen=True)
class BenchmarkQuery:
    id: str
    user_id: str
    query: str | list[dict[str, Any]]
    relevant_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkDataset:
    version: str
    items: tuple[BenchmarkItem, ...]
    queries: tuple[BenchmarkQuery, ...]


@dataclass(frozen=True)
class ExperimentMetrics:
    recall_at_10: float
    recall_at_100: float
    mrr: float
    ndcg: float
    mean_latency_ms: float
    p95_latency_ms: float
    cost_usd: float
    model_calls: int
    degradation_rate: float


@dataclass(frozen=True)
class ExperimentManifest:
    schema_version: int
    experiment_name: str
    run_id: str
    git_commit: str
    seed: int
    dataset_adapter: str
    dataset_version: str
    config_sha256: str
    models: dict[str, str]
    prompts: dict[str, str]
    item_count: int
    query_count: int
    metrics: ExperimentMetrics


@dataclass(frozen=True)
class ComparisonRow:
    experiment_name: str
    run_id: str
    recall_at_10: float
    recall_at_100: float
    mrr: float
    ndcg: float
    mean_latency_ms: float
    cost_usd: float
    model_calls: int
    degradation_rate: float


@dataclass(frozen=True)
class ComparisonReport:
    rows: tuple[ComparisonRow, ...]
    best_recall_at_10: str | None
    best_ndcg: str | None
