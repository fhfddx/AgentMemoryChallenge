"""与具体在线实现解耦的检索与运行成本指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean

from experiments.models import ExperimentMetrics


@dataclass(frozen=True)
class QueryRanking:
    """一条查询的返回顺序；True 表示该位置命中一个相关条目。"""

    relevant: tuple[bool, ...]
    total_relevant: int


def _recall(ranking: QueryRanking, cutoff: int) -> float:
    if ranking.total_relevant <= 0:
        return 0.0
    hits = sum(ranking.relevant[:cutoff])
    return min(1.0, hits / ranking.total_relevant)


def _reciprocal_rank(ranking: QueryRanking) -> float:
    for index, relevant in enumerate(ranking.relevant, start=1):
        if relevant:
            return 1.0 / index
    return 0.0


def _ndcg(ranking: QueryRanking) -> float:
    if ranking.total_relevant <= 0:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, relevant in enumerate(ranking.relevant, start=1)
        if relevant
    )
    ideal_hits = min(ranking.total_relevant, len(ranking.relevant))
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def calculate_metrics(
    rankings: list[QueryRanking],
    *,
    latency_ms: list[float],
    cost_usd: float,
    model_calls: int,
    degraded_operations: int,
    total_operations: int,
) -> ExperimentMetrics:
    """计算固定定义的检索、延迟、成本、调用与降级指标。"""
    recall_10 = fmean(_recall(ranking, 10) for ranking in rankings) if rankings else 0.0
    recall_100 = fmean(_recall(ranking, 100) for ranking in rankings) if rankings else 0.0
    mrr = fmean(_reciprocal_rank(ranking) for ranking in rankings) if rankings else 0.0
    ndcg = fmean(_ndcg(ranking) for ranking in rankings) if rankings else 0.0
    degradation_rate = degraded_operations / total_operations if total_operations else 0.0
    return ExperimentMetrics(
        recall_at_10=recall_10,
        recall_at_100=recall_100,
        mrr=mrr,
        ndcg=ndcg,
        mean_latency_ms=fmean(latency_ms) if latency_ms else 0.0,
        p95_latency_ms=_p95(latency_ms),
        cost_usd=cost_usd,
        model_calls=model_calls,
        degradation_rate=degradation_rate,
    )
