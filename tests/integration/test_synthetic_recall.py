"""合成评估的能力分类、Recall 计分与脱敏报告测试。"""

import json
import logging

import pytest
from scripts.evaluate_synthetic_recall import (
    aggregate_results,
    build_cases,
    run_version,
    score_search_case,
)


def test_synthetic_recall_by_category() -> None:
    cases = build_cases("fixed-run")
    categories = {case.category for case in cases}
    assert categories >= {
        "direct_recall", "multi_fact", "single_message_multi_fact", "cross_session", "text_image",
        "image_only", "abstention", "top100",
    }

    by_category = {case.category: case for case in cases}
    direct = by_category["direct_recall"]
    multi = by_category["multi_fact"]
    single_message = by_category["single_message_multi_fact"]
    empty = by_category["abstention"]

    assert len(single_message.additions) == 1
    assert len(single_message.additions[0]) == 1
    assert all(
        marker in single_message.additions[0][0]["content"]
        for marker in single_message.expected_markers
    )

    direct_score = score_search_case(direct, [{"content": direct.expected_markers[0]}])
    partial_score = score_search_case(multi, [{"content": multi.expected_markers[0]}])
    empty_score = score_search_case(empty, [])

    assert direct_score["recall_at_10"] == 1.0
    assert direct_score["coverage"] == 1.0
    assert partial_score["recall_at_10"] == 0.5
    assert partial_score["coverage"] == 0.5
    combined = score_search_case(
        multi, [{"content": " ".join(multi.expected_markers)}]
    )
    assert combined["recall_at_10"] == 1.0
    assert combined["atomic_coverage_at_10"] == 0.0
    split = score_search_case(
        multi, [{"content": marker} for marker in multi.expected_markers]
    )
    assert split["atomic_coverage_at_10"] == 1.0
    assert empty_score["empty_result"] is True
    report = aggregate_results(
        {"direct_recall": [direct_score], "multi_fact": [partial_score]},
        add_latencies=[10.0, 20.0], search_latencies=[5.0, 15.0],
    )
    assert report["categories"]["multi_fact"]["recall_at_10"] == 0.5
    assert report["categories"]["multi_fact"]["atomic_coverage_at_10"] == 0.5
    assert report["latency_ms"]["add"]["mean"] == 15.0
    assert report["cost"] == "unavailable"
    assert "fixed-run" not in str(report)


def test_cleanup_id_is_recorded_before_failing_add(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Health:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "ok"}

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _path: str) -> _Health:
            return _Health()

        def post(self, _path: str, *, json: object) -> None:
            raise RuntimeError("network failure")

    monkeypatch.setattr(
        "scripts.evaluate_synthetic_recall.httpx.Client", lambda **_kwargs: _FailingClient()
    )
    recorded: list[tuple[str, str, str]] = []

    with pytest.raises(RuntimeError, match="network failure"):
        run_version(
            "http://127.0.0.1:8000", "test-key", build_cases("fixed-run")[:1],
            version="v1.1", run_tag="fixed-run", timeout=5,
            register_cleanup=recorded.append,
        )

    assert recorded == [
        ("v1.1", "synthetic-fixed-run-v1.1-direct_recall",
         "synthetic-fixed-run-v1.1-direct_recall-0")
    ]


def test_search_error_diagnostic_omits_payload(client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="masm.search"):
        response = client.post(
            "/search",
            headers={"X-Api-Key": "wrong-key-secret"},
            json={"query": "query-secret", "user_id": "user-secret", "top_k": 10},
        )

    assert response.status_code == 401
    assert not any(
        value in caplog.text for value in ("wrong-key-secret", "query-secret", "user-secret")
    )
    assert caplog.records, {
        "root_handlers": [type(handler).__name__ for handler in logging.getLogger().handlers],
        "capture_attached": caplog.handler in logging.getLogger().handlers,
        "global_disable": logging.root.manager.disable,
    }
    payload = json.loads(caplog.records[-1].message)
    assert payload["status_code"] == 401
    assert payload["candidate_count"] == 0
