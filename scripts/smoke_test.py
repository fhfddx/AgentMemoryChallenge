"""仅通过公共 HTTP 接口执行 MASM 候选版本 Smoke 验证。"""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

import httpx


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MASM 公共 API Smoke 验证")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--verify-request-id")
    parser.add_argument("--verify-user-id")
    parser.add_argument("--verify-marker")
    return parser


def _require_success(response: httpx.Response, operation: str) -> dict:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"{operation} 失败，HTTP {response.status_code}") from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise SystemExit(f"{operation} 返回格式错误")
    return payload


def _search(client: httpx.Client, headers: dict[str, str], user_id: str, marker: str) -> dict:
    response = client.post(
        "/search",
        headers=headers,
        json={"query": marker, "user_id": user_id, "top_k": 10},
    )
    payload = _require_success(response, "Search")
    data = payload.get("data")
    if not isinstance(data, list) or not any(marker in str(item.get("content")) for item in data):
        raise SystemExit("Search 未找到刚写入的固定标记")
    return payload


def main() -> int:
    args = _parser().parse_args()
    headers = {"X-Api-Key": args.api_key}
    verify_values = (args.verify_request_id, args.verify_user_id, args.verify_marker)
    if any(verify_values) and not all(verify_values):
        raise SystemExit("重启验证的三个 --verify-* 参数必须同时提供")

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.timeout) as client:
        health = _require_success(client.get("/health"), "Health")
        if health != {"status": "ok"}:
            raise SystemExit("Health 返回内容不符合契约")

        if all(verify_values):
            _search(client, headers, args.verify_user_id, args.verify_marker)
            print(json.dumps({"status": "ok", "phase": "restart-verify"}))
            return 0

        marker = f"smoke-{uuid4().hex}"
        user_id = f"smoke-user-{uuid4().hex}"
        request_id = f"smoke-request-{uuid4().hex}"
        add_payload = {
            "request_id": request_id,
            "user_id": user_id,
            "session_id": f"smoke-session-{uuid4().hex}",
            "messages": [{"role": "user", "content": marker}],
        }
        first = _require_success(client.post("/add", headers=headers, json=add_payload), "Add")
        _search(client, headers, user_id, marker)
        repeated = _require_success(
            client.post("/add", headers=headers, json=add_payload), "重复 Add"
        )
        if repeated != first:
            raise SystemExit("重复 Add 未返回相同幂等结果")

    print(
        json.dumps(
            {
                "status": "ok",
                "phase": "initial",
                "request_id": request_id,
                "user_id": user_id,
                "marker": marker,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
