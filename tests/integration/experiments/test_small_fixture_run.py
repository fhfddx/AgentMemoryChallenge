"""固定小样例实验：只经由 Add/Search 形状的公共客户端。"""

import json
from pathlib import Path

from experiments.run_experiment import TimedResponse, run_experiment


class _FakePublicApi:
    """只实现公开 Add/Search，不暴露仓库或答案查询。"""

    def __init__(self) -> None:
        self._content_by_user: dict[str, list[str]] = {}

    def add(self, payload: dict) -> TimedResponse:
        user_id = payload["user_id"]
        content = str(payload["messages"][0]["content"])
        self._content_by_user.setdefault(user_id, [])
        if content not in self._content_by_user[user_id]:
            self._content_by_user[user_id].append(content)
        return TimedResponse(
            payload={
                "success": True,
                "request_id": payload["request_id"],
                "user_id": user_id,
                "session_id": payload["session_id"],
            },
            latency_ms=2.0,
        )

    def search(self, payload: dict) -> TimedResponse:
        query = str(payload["query"])
        contents = self._content_by_user.get(payload["user_id"], [])
        ranked = sorted(contents, key=lambda content: query not in content)
        return TimedResponse(
            payload={
                "data": [
                    {"id": f"evidence-{index}", "content": content, "score": 1.0}
                    for index, content in enumerate(ranked)
                ]
            },
            latency_ms=3.0,
        )

    def close(self) -> None:
        return None


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    dataset = {
        "version": "fixture-v1",
        "items": [
            {
                "id": "item-blue",
                "user_id": "fixture-user",
                "session_id": "fixture-session",
                "messages": [{"role": "user", "content": "blue-lantern is stored"}],
                "match_token": "blue-lantern",
            },
            {
                "id": "item-red",
                "user_id": "fixture-user",
                "session_id": "fixture-session",
                "messages": [{"role": "user", "content": "red-harbor is stored"}],
                "match_token": "red-harbor",
            },
        ],
        "queries": [
            {
                "id": "query-blue",
                "user_id": "fixture-user",
                "query": "blue-lantern",
                "relevant_item_ids": ["item-blue"],
            }
        ],
    }
    dataset_path = tmp_path / "fixture.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    config = {
        "schema_version": 1,
        "name": "fixture-b0",
        "seed": 7,
        "git_commit": "auto",
        "base_url": "http://fixture.invalid",
        "api_key_env": "MASM_API_KEY",
        "dataset": {
            "adapter": "atm_bench",
            "path": str(dataset_path),
            "version": "fixture-v1",
        },
        "models": {"embedding": "fake-v1", "llm": "none", "reranker": "none"},
        "prompts": {"perception": "disabled", "temporal": "disabled", "curator": "disabled"},
        "accounting": {
            "cost_per_add_usd": 0.0,
            "cost_per_search_usd": 0.0,
            "model_calls_per_add": 1,
            "model_calls_per_search": 1,
            "degraded_operations": 0,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path, dataset_path


def test_same_fixture_and_seed_produce_identical_manifest_without_raw_text(
    tmp_path: Path,
) -> None:
    config_path, _ = _write_fixture(tmp_path)

    first = run_experiment(config_path, tmp_path / "run-1", client=_FakePublicApi())
    second = run_experiment(config_path, tmp_path / "run-2", client=_FakePublicApi())

    assert first == second
    assert (tmp_path / "run-1" / "manifest.json").read_bytes() == (
        tmp_path / "run-2" / "manifest.json"
    ).read_bytes()
    serialized = (tmp_path / "run-1" / "manifest.json").read_text(encoding="utf-8")
    assert "blue-lantern is stored" not in serialized
    assert "red-harbor is stored" not in serialized
    assert first.metrics.recall_at_10 == 1.0
    assert first.metrics.recall_at_100 == 1.0
    assert first.metrics.model_calls == 3
    assert first.dataset_version == "fixture-v1"
