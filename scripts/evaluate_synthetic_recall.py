"""在两套本地 Add/Search 服务上运行固定小规模合成评估。

仅输出聚合指标；API Key 从环境变量读取，正文和图片不会写入报告或控制台。
本脚本不自动清理运行数据，单独保存权限收紧的删除清单。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image


@dataclass(frozen=True)
class EvaluationCase:
    category: str
    additions: tuple[tuple[dict[str, Any], ...], ...]
    query: str | list[dict[str, Any]]
    expected_markers: tuple[str, ...]
    top_k: int = 10
    expect_nonempty: bool = False
    expect_empty: bool = False


def _image_url() -> str:
    image = Image.new("RGB", (8, 8), (20, 70, 180))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def build_cases(run_tag: str) -> list[EvaluationCase]:
    """固定语料与类别；两套版本使用同样的内容和查询。"""
    prefix = f"synth-{run_tag}"
    image_part = {"type": "image_url", "image_url": {"url": _image_url()}}
    direct = f"{prefix}-direct"
    fact_a, fact_b = f"{prefix}-fact-a", f"{prefix}-fact-b"
    session_a, session_b = f"{prefix}-session-a", f"{prefix}-session-b"
    text_image = f"{prefix}-text-image"
    top_markers = tuple(f"{prefix}-item-{index:03}" for index in range(100))
    return [
        EvaluationCase(
            "direct_recall", (({"role": "user", "content": f"The marker is {direct}."},),),
            direct, (direct,),
        ),
        EvaluationCase(
            "multi_fact", ((
                {"role": "user", "content": f"First fact: {fact_a}."},
                {"role": "assistant", "content": f"Second fact: {fact_b}."},
            ),), f"{fact_a} {fact_b}", (fact_a, fact_b),
        ),
        EvaluationCase(
            "cross_session", (
                ({"role": "user", "content": f"Earlier: {session_a}."},),
                ({"role": "user", "content": f"Later: {session_b}."},),
            ), f"{session_a} {session_b}", (session_a, session_b),
        ),
        EvaluationCase(
            "text_image", (({"role": "user", "content": [
                {"type": "text", "text": text_image}, image_part,
            ]},),), text_image, (text_image,),
        ),
        EvaluationCase(
            "image_only", (({"role": "user", "content": [image_part]},),),
            [image_part], (), expect_nonempty=True,
        ),
        EvaluationCase("abstention", (), f"{prefix}-absent", (), expect_empty=True),
        EvaluationCase(
            "top100", (tuple(
                {"role": "user", "content": f"Item {index}: {marker}."}
                for index, marker in enumerate(top_markers)
            ),), f"{prefix}-item", top_markers, top_k=100,
        ),
    ]


def score_search_case(
    case: EvaluationCase, data: list[dict[str, Any]], *, response_bytes: int = 0
) -> dict[str, Any]:
    """按预注册标记计算证据覆盖；不输出证据正文。"""
    contents = [str(item.get("content", "")) for item in data]
    if case.expected_markers:
        denominator = len(case.expected_markers)
        recall_10 = sum(
            any(marker in content for content in contents[:10])
            for marker in case.expected_markers
        ) / denominator
        recall_100 = sum(
            any(marker in content for content in contents[:100])
            for marker in case.expected_markers
        ) / denominator
        atomic_10 = sum(
            any(
                marker in content
                and sum(other in content for other in case.expected_markers) == 1
                for content in contents[:10]
            )
            for marker in case.expected_markers
        ) / denominator
        atomic_100 = sum(
            any(
                marker in content
                and sum(other in content for other in case.expected_markers) == 1
                for content in contents[:100]
            )
            for marker in case.expected_markers
        ) / denominator
    elif case.expect_empty:
        recall_10 = recall_100 = float(not data)
        atomic_10 = atomic_100 = recall_10
    else:
        recall_10 = recall_100 = float(bool(data))
        atomic_10 = atomic_100 = recall_10
    return {
        "recall_at_10": round(recall_10, 4),
        "recall_at_100": round(recall_100, 4),
        "coverage": round(recall_100, 4),
        "atomic_coverage_at_10": round(atomic_10, 4),
        "atomic_coverage_at_100": round(atomic_100, 4),
        "empty_result": not data,
        "returned_count": len(data),
        "response_bytes": response_bytes,
    }


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p95": None}
    ordered = sorted(values)
    return {
        "mean": round(statistics.mean(values), 2),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
    }


def aggregate_results(
    scores: dict[str, list[dict[str, Any]]],
    *,
    add_latencies: list[float],
    search_latencies: list[float],
) -> dict[str, Any]:
    """只产出按能力类别的数值；不可观测模型成本明确标为 unavailable。"""
    categories: dict[str, dict[str, float | int]] = {}
    for category, entries in sorted(scores.items()):
        categories[category] = {
            "cases": len(entries),
            "recall_at_10": round(statistics.mean(item["recall_at_10"] for item in entries), 4),
            "recall_at_100": round(statistics.mean(item["recall_at_100"] for item in entries), 4),
            "coverage": round(statistics.mean(item["coverage"] for item in entries), 4),
            "atomic_coverage_at_10": round(
                statistics.mean(item["atomic_coverage_at_10"] for item in entries), 4
            ),
            "atomic_coverage_at_100": round(
                statistics.mean(item["atomic_coverage_at_100"] for item in entries), 4
            ),
            "empty_result_rate": round(
                statistics.mean(item["empty_result"] for item in entries), 4
            ),
            "mean_returned": round(statistics.mean(item["returned_count"] for item in entries), 2),
            "max_response_bytes": max(item["response_bytes"] for item in entries),
        }
    return {
        "categories": categories,
        "latency_ms": {
            "add": _latency_summary(add_latencies),
            "search": _latency_summary(search_latencies),
        },
        "embedding_inputs": "unavailable",
        "llm_calls": "unavailable",
        "perception_calls": "unavailable",
        "cost": "unavailable",
    }


def _require_local(url: str, *, allow_remote: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("服务地址无效")
    if not allow_remote and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("默认只允许本机服务；远程评估需要显式 --allow-remote")


def _post(
    client: httpx.Client, path: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    response = client.post(path, json=payload)
    latency_ms = (time.perf_counter() - started) * 1000.0
    if response.status_code != 200:
        raise RuntimeError(f"{path} HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} 响应格式错误")
    return body, latency_ms, len(response.content)


def run_version(
    url: str,
    key: str,
    cases: list[EvaluationCase],
    *,
    version: str,
    run_tag: str,
    timeout: float,
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """只通过公开 HTTP 接口评估；异常不包含正文或密钥。"""
    scores: dict[str, list[dict[str, Any]]] = {}
    add_latencies: list[float] = []
    search_latencies: list[float] = []
    cleanup: list[tuple[str, str, str]] = []
    with httpx.Client(
        base_url=url.rstrip("/"), headers={"X-Api-Key": key}, timeout=timeout
    ) as client:
        health = client.get("/health")
        if health.status_code != 200 or health.json() != {"status": "ok"}:
            raise RuntimeError("Health 检查失败")
        for case in cases:
            user_id = f"synthetic-{run_tag}-{version}-{case.category}"
            for index, messages in enumerate(case.additions):
                request_id = f"synthetic-{run_tag}-{version}-{case.category}-{index}"
                cleanup.append((version, user_id, request_id))
                body, latency, _bytes = _post(client, "/add", {
                    "request_id": request_id,
                    "user_id": user_id,
                    "session_id": f"synthetic-{run_tag}-{case.category}-session-{index}",
                    "messages": list(messages),
                })
                if body.get("success") is not True:
                    raise RuntimeError("Add 契约错误")
                add_latencies.append(latency)
            body, latency, response_bytes = _post(client, "/search", {
                "query": case.query, "user_id": user_id, "top_k": case.top_k,
            })
            data = body.get("data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise RuntimeError("Search 契约错误")
            if len(data) > case.top_k:
                raise RuntimeError("Search 超过 top_k")
            if response_bytes > 30 * 1024 * 1024:
                raise RuntimeError("Search 响应超过 30 MiB")
            search_latencies.append(latency)
            scores.setdefault(case.category, []).append(
                score_search_case(case, data, response_bytes=response_bytes)
            )
    return aggregate_results(
        scores, add_latencies=add_latencies, search_latencies=search_latencies
    ), cleanup


def main() -> int:
    parser = argparse.ArgumentParser(description="MASM 本地合成召回对照")
    parser.add_argument("--v10-url", required=True)
    parser.add_argument("--v11-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup-output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    for url in (args.v10_url, args.v11_url):
        _require_local(url, allow_remote=args.allow_remote)
    keys = {
        "v1.0": os.getenv("MASM_V10_API_KEY", ""),
        "v1.1": os.getenv("MASM_V11_API_KEY", ""),
    }
    if not all(keys.values()):
        raise SystemExit("请通过 MASM_V10_API_KEY 和 MASM_V11_API_KEY 环境变量提供测试密钥")
    run_tag = uuid4().hex[:12]
    cases = build_cases(run_tag)
    reports: dict[str, dict[str, Any]] = {}
    cleanup: list[tuple[str, str, str]] = []
    for version, url in (("v1.0", args.v10_url), ("v1.1", args.v11_url)):
        report, rows = run_version(
            url, keys[version], cases, version=version, run_tag=run_tag,
            timeout=args.timeout,
        )
        reports[version] = report
        cleanup.extend(rows)
    del keys
    deltas = {
        category: round(
            reports["v1.1"]["categories"][category]["recall_at_10"]
            - reports["v1.0"]["categories"][category]["recall_at_10"], 4
        )
        for category in reports["v1.0"]["categories"]
    }
    output = {"versions": reports, "recall_at_10_delta": deltas, "case_count": len(cases)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    args.cleanup_output.parent.mkdir(parents=True, exist_ok=True)
    args.cleanup_output.write_text(
        "".join("\t".join(row) + "\n" for row in cleanup), encoding="utf-8"
    )
    args.cleanup_output.chmod(0o600)
    print(json.dumps({"status": "ok", "case_count": len(cases)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
