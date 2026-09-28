"""实验指标的手算样例。"""

import math

import pytest
from experiments.metrics import QueryRanking, calculate_metrics


def test_metric_calculation_matches_hand_computed_values() -> None:
    rankings = [
        QueryRanking(relevant=(True, False, False), total_relevant=2),
        QueryRanking(relevant=(False, True, False), total_relevant=1),
    ]

    metrics = calculate_metrics(
        rankings,
        latency_ms=[10.0, 30.0],
        cost_usd=0.0125,
        model_calls=5,
        degraded_operations=1,
        total_operations=4,
    )

    assert metrics.recall_at_10 == pytest.approx(0.75)
    assert metrics.recall_at_100 == pytest.approx(0.75)
    assert metrics.mrr == pytest.approx(0.75)
    expected_ndcg = (1 / (1 + 1 / math.log2(3)) + (1 / math.log2(3))) / 2
    assert metrics.ndcg == pytest.approx(expected_ndcg)
    assert metrics.mean_latency_ms == pytest.approx(20.0)
    assert metrics.p95_latency_ms == pytest.approx(30.0)
    assert metrics.cost_usd == pytest.approx(0.0125)
    assert metrics.model_calls == 5
    assert metrics.degradation_rate == pytest.approx(0.25)


def test_empty_rankings_produce_zero_retrieval_metrics() -> None:
    metrics = calculate_metrics(
        [],
        latency_ms=[],
        cost_usd=0.0,
        model_calls=0,
        degraded_operations=0,
        total_operations=0,
    )

    assert metrics.recall_at_10 == 0.0
    assert metrics.recall_at_100 == 0.0
    assert metrics.mrr == 0.0
    assert metrics.ndcg == 0.0
    assert metrics.mean_latency_ms == 0.0
    assert metrics.p95_latency_ms == 0.0
    assert metrics.degradation_rate == 0.0
