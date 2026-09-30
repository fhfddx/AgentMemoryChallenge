"""通过公网 Add/Search 接口执行小规模并发容量测试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


@dataclass(frozen=True)
class RequestResult:
    """单次 HTTP 请求的脱敏结果。"""

    status: int
    latency_ms: float
    contract_ok: bool
    error: str | None = None


def parse_levels(raw: str) -> tuple[int, ...]:
    """解析并发级别，去重并限制在当前服务的 1..10 范围。"""
    try:
        values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError("并发级别必须是逗号分隔的整数") from exc
    if not values or any(value < 1 or value > 10 for value in values):
        raise ValueError("并发级别必须位于 1 到 10")
    return tuple(dict.fromkeys(values))


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index], 2)


def summarize_results(
    operation: str,
    concurrency: int,
    results: Sequence[RequestResult],
    *,
    elapsed_ms: float,
) -> dict[str, object]:
    """生成不含请求正文或凭据的容量统计。"""
    latencies = [result.latency_ms for result in results]
    statuses: dict[str, int] = {}
    for result in results:
        key = str(result.status)
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "phase": "capacity",
        "operation": operation,
        "concurrency": concurrency,
        "requests": len(results),
        "elapsed_ms": round(elapsed_ms, 2),
        "mean_ms": round(statistics.mean(latencies), 2),
        "p95_ms": _p95(latencies),
        "contract_ok": sum(1 for result in results if result.contract_ok),
        "statuses": statuses,
        "errors": [result.error for result in results if result.error is not None],
    }


def write_json_line(path: Path, payload: dict[str, object]) -> None:
    """追加一条 JSONL 证据并收紧文件权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    path.chmod(0o600)


def write_cleanup_manifest(path: Path, records: Iterable[tuple[str, str]]) -> None:
    """写出 user_id/request_id 清理清单，不记录任何请求正文或密钥。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{user_id}\t{request_id}\n" for user_id, request_id in records),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _emit(path: Path, payload: dict[str, object]) -> None:
    write_json_line(path, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


async def _request(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    expected_marker: str | None = None,
) -> RequestResult:
    started = time.perf_counter()
    try:
        response = await client.post(path, json=payload)
        latency_ms = (time.perf_counter() - started) * 1000.0
        contract_ok = False
        if response.status_code == 200:
            body = response.json()
            if path == "/add":
                contract_ok = (
                    isinstance(body, dict)
                    and body.get("success") is True
                    and body.get("request_id") == payload["request_id"]
                    and body.get("user_id") == payload["user_id"]
                    and body.get("session_id") == payload["session_id"]
                )
            else:
                data = body.get("data") if isinstance(body, dict) else None
                contract_ok = isinstance(data, list) and len(data) <= int(payload["top_k"])
                if contract_ok and expected_marker is not None:
                    contract_ok = any(
                        expected_marker in str(item.get("content"))
                        for item in data
                        if isinstance(item, dict)
                    )
        return RequestResult(
            status=response.status_code,
            latency_ms=latency_ms,
            contract_ok=contract_ok,
        )
    except Exception as exc:  # 网络边界只记录异常类型，避免正文进入证据。
        return RequestResult(
            status=0,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            contract_ok=False,
            error=type(exc).__name__,
        )


async def run_capacity_test(args: argparse.Namespace, api_key: str) -> None:
    """运行一次容量测试并写出脱敏结果与清理清单。"""
    levels = parse_levels(args.levels)
    output_path = args.output.resolve()
    cleanup_path = args.cleanup_output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    output_path.chmod(0o600)

    test_id = uuid4().hex
    cleanup_rows: list[tuple[str, str]] = []
    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 15.0))
    headers = {"X-Api-Key": api_key}

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
    ) as client:
        health = await client.get("/health")
        health.raise_for_status()
        if health.json() != {"status": "ok"}:
            raise RuntimeError("Health 响应格式错误")
        _emit(output_path, {"phase": "health", "status": "ok", "test_id": test_id})

        seed_user = f"capacity-search-user-{test_id}"
        seed_request = f"capacity-search-seed-{test_id}"
        seed_marker = f"capacity-marker-{test_id}"
        seed_payload = {
            "request_id": seed_request,
            "user_id": seed_user,
            "session_id": f"capacity-search-session-{test_id}",
            "messages": [{"role": "user", "content": seed_marker}],
        }
        cleanup_rows.append((seed_user, seed_request))
        write_cleanup_manifest(cleanup_path, cleanup_rows)
        seed = await _request(client, "/add", seed_payload)
        if seed.status != 200 or not seed.contract_ok:
            raise RuntimeError(f"Search 种子 Add 失败，HTTP {seed.status}")
        _emit(
            output_path,
            {
                "phase": "search_seed",
                "status": "ok",
                "latency_ms": round(seed.latency_ms, 2),
            },
        )

        for concurrency in levels:
            payloads: list[dict[str, Any]] = []
            for index in range(concurrency):
                user_id = f"capacity-add-user-{test_id}-{concurrency}-{index}"
                request_id = f"capacity-add-request-{test_id}-{concurrency}-{index}"
                cleanup_rows.append((user_id, request_id))
                payloads.append(
                    {
                        "request_id": request_id,
                        "user_id": user_id,
                        "session_id": f"capacity-add-session-{test_id}-{concurrency}-{index}",
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Synthetic capacity test memory "
                                    f"{test_id}-{concurrency}-{index}."
                                ),
                            }
                        ],
                    }
                )
            write_cleanup_manifest(cleanup_path, cleanup_rows)
            started = time.perf_counter()
            results = await asyncio.gather(
                *(_request(client, "/add", payload) for payload in payloads)
            )
            summary = summarize_results(
                "add",
                concurrency,
                results,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            _emit(output_path, summary)

        search_payload = {"query": seed_marker, "user_id": seed_user, "top_k": 100}
        for concurrency in levels:
            started = time.perf_counter()
            results = await asyncio.gather(
                *(
                    _request(
                        client,
                        "/search",
                        search_payload,
                        expected_marker=seed_marker,
                    )
                    for _ in range(concurrency)
                )
            )
            summary = summarize_results(
                "search",
                concurrency,
                results,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            _emit(output_path, summary)

    _emit(
        output_path,
        {
            "phase": "complete",
            "status": "ok",
            "test_id": test_id,
            "cleanup_records": len(cleanup_rows),
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MASM 公网 Add/Search 容量测试")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-env", default="MASM_COMPETITION_KEY")
    parser.add_argument("--levels", default="1,2,4,8")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup-output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(f"缺少 API Key 环境变量: {args.api_key_env}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run_capacity_test(args, api_key))
    except Exception as exc:
        failure = {"phase": "failed", "status": "error", "error": type(exc).__name__}
        write_json_line(args.output.resolve(), failure)
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
