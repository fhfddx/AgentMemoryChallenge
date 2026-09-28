"""速率与并发限制契约测试。"""

from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from masm.api.app import create_app
from masm.api.limits import RequestLimiter
from masm.storage.assets import AssetStore
from masm.storage.db import Database


def _payload(user_id: str) -> dict:
    return {
        "request_id": f"req-{uuid4().hex}",
        "user_id": user_id,
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "hello"}],
    }


@pytest.mark.asyncio
async def test_limiter_rejects_over_concurrency() -> None:
    """超过并发上限时抛出带 Retry-After 的 429。"""
    limiter = RequestLimiter(max_concurrent=1, retry_after_seconds=7)
    async with limiter.acquire("key-1"):
        with pytest.raises(HTTPException) as excinfo:
            async with limiter.acquire("key-1"):
                pass
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "7"


@pytest.mark.asyncio
async def test_limiter_allows_after_release() -> None:
    """释放槽位后可再次获取。"""
    limiter = RequestLimiter(max_concurrent=1)
    async with limiter.acquire("key-1"):
        pass
    async with limiter.acquire("key-1"):
        pass


@pytest.mark.asyncio
async def test_add_returns_429_with_retry_after(
    settings, database: Database, asset_store: AssetStore
) -> None:
    """并发槽位被占用时 /add 返回 429 且带 Retry-After。"""
    limiter = RequestLimiter(max_concurrent=1, retry_after_seconds=5)
    app = create_app(settings, database=database, asset_store=asset_store, limiter=limiter)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with limiter.acquire("test-key"):
            response = await client.post(
                "/add", json=_payload(f"u-{uuid4().hex}"), headers={"X-Api-Key": "test-key"}
            )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
