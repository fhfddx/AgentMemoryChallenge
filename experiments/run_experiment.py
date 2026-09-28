"""通过公共 Add/Search HTTP 接口运行可复现实验。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from experiments.adapters import atm_bench, mem_gallery
from experiments.metrics import QueryRanking, calculate_metrics
from experiments.models import BenchmarkDataset, ExperimentManifest


@dataclass(frozen=True)
class TimedResponse:
    payload: dict[str, Any]
    latency_ms: float


class PublicMemoryClient(Protocol):
    def add(self, payload: dict[str, Any]) -> TimedResponse: ...

    def search(self, payload: dict[str, Any]) -> TimedResponse: ...


class HttpPublicMemoryClient:
    """只暴露官方 Add/Search 的 HTTP 客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def _post(self, path: str, payload: dict[str, Any]) -> TimedResponse:
        started = time.perf_counter()
        response = self._client.post(path, json=payload)
        latency_ms = (time.perf_counter() - started) * 1000.0
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"{path} 返回值不是 JSON 对象")
        return TimedResponse(payload=body, latency_ms=latency_ms)

    def add(self, payload: dict[str, Any]) -> TimedResponse:
        return self._post("/add", payload)

    def search(self, payload: dict[str, Any]) -> TimedResponse:
        return self._post("/search", payload)

    def close(self) -> None:
        self._client.close()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _git_commit(configured: str) -> str:
    if configured != "auto":
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_dataset(adapter: str, path: Path) -> BenchmarkDataset:
    if adapter == "atm_bench":
        return atm_bench.load_dataset(path)
    if adapter == "mem_gallery":
        return mem_gallery.load_dataset(path)
    raise ValueError(f"不支持的数据适配器: {adapter}")


def _evidence_text(content: Any) -> str:
    return content if isinstance(content, str) else _canonical_json(content)


def run_experiment(
    config_path: Path,
    output_dir: Path,
    *,
    client: PublicMemoryClient | None = None,
) -> ExperimentManifest:
    """执行一次实验并写出确定性清单；被测系统只通过公共客户端访问。"""
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    placeholders = [
        key for key, value in config["models"].items() if str(value) == "record-before-run"
    ]
    if placeholders:
        raise ValueError(f"正式运行前必须填写模型版本: {', '.join(sorted(placeholders))}")
    dataset_config = config["dataset"]
    dataset_path = Path(dataset_config["path"])
    if not dataset_path.is_absolute():
        dataset_path = (config_path.parent / dataset_path).resolve()
    dataset = _load_dataset(str(dataset_config["adapter"]), dataset_path)
    if dataset.version != str(dataset_config["version"]):
        raise ValueError("配置的数据集版本与样例文件不一致")

    owned_client: HttpPublicMemoryClient | None = None
    if client is None:
        key_name = str(config["api_key_env"])
        api_key = os.environ.get(key_name)
        if not api_key:
            raise RuntimeError(f"缺少 API Key 环境变量: {key_name}")
        owned_client = HttpPublicMemoryClient(str(config["base_url"]), api_key)
        client = owned_client

    latencies: list[float] = []
    rankings: list[QueryRanking] = []
    tokens_by_item = {item.id: item.match_token for item in dataset.items}
    try:
        for item in dataset.items:
            result = client.add(
                {
                    "request_id": f"{config['name']}:{config['seed']}:{item.id}",
                    "user_id": item.user_id,
                    "session_id": item.session_id,
                    "messages": list(item.messages),
                }
            )
            if result.payload.get("success") is not True:
                raise ValueError("Add 未返回 success=true")
            latencies.append(result.latency_ms)

        for query in dataset.queries:
            result = client.search(
                {
                    "query": query.query,
                    "user_id": query.user_id,
                    "top_k": 100,
                }
            )
            latencies.append(result.latency_ms)
            evidence = result.payload.get("data")
            if not isinstance(evidence, list):
                raise ValueError("Search 未返回 data 数组")
            relevant_tokens = {tokens_by_item[item_id] for item_id in query.relevant_item_ids}
            flags = tuple(
                any(token in _evidence_text(item.get("content")) for token in relevant_tokens)
                for item in evidence
                if isinstance(item, dict)
            )
            rankings.append(QueryRanking(flags, len(query.relevant_item_ids)))
    finally:
        if owned_client is not None:
            owned_client.close()

    accounting = config["accounting"]
    add_count = len(dataset.items)
    search_count = len(dataset.queries)
    cost = (
        add_count * float(accounting["cost_per_add_usd"])
        + search_count * float(accounting["cost_per_search_usd"])
    )
    model_calls = (
        add_count * int(accounting["model_calls_per_add"])
        + search_count * int(accounting["model_calls_per_search"])
    )
    metrics = calculate_metrics(
        rankings,
        latency_ms=latencies,
        cost_usd=cost,
        model_calls=model_calls,
        degraded_operations=int(accounting["degraded_operations"]),
        total_operations=add_count + search_count,
    )
    canonical_config = _canonical_json(config)
    config_hash = hashlib.sha256(canonical_config.encode("utf-8")).hexdigest()
    run_id = hashlib.sha256(
        f"{config_hash}:{dataset.version}:{config['seed']}".encode()
    ).hexdigest()[:16]
    manifest = ExperimentManifest(
        schema_version=1,
        experiment_name=str(config["name"]),
        run_id=run_id,
        git_commit=_git_commit(str(config["git_commit"])),
        seed=int(config["seed"]),
        dataset_adapter=str(dataset_config["adapter"]),
        dataset_version=dataset.version,
        config_sha256=config_hash,
        models={str(key): str(value) for key, value in config["models"].items()},
        prompts={str(key): str(value) for key, value in config["prompts"].items()},
        item_count=add_count,
        query_count=search_count,
        metrics=metrics,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 MASM 公开 API 实验")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = run_experiment(args.config, args.output)
    print(json.dumps({"run_id": manifest.run_id, "status": "ok"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
