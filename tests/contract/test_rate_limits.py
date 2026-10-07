"""速率与并发限制契约测试。"""

from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from masm.api.app import build_request_limiter, create_app
from masm.api.auth import credential_fingerprint
from masm.api.limits import RequestLimiter
from masm.config import Settings
from masm.storage.assets import AssetStore
from masm.storage.db import Database


class _FakeTimer:
    """可注入的单调时钟，测试无需真实 sleep。"""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _payload(user_id: str) -> dict:
    return {
        "request_id": f"req-{uuid4().hex}",
        "user_id": user_id,
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "hello"}],
    }


@pytest.mark.asyncio
async def test_default_app_limiter_accepts_declared_sixteen_concurrent_requests() -> None:
    """官网按声明发起 16 个并发请求时，应用不得在第 11 个提前返回 429。"""
    configured = Settings(database_url="postgresql+psycopg://unused/masm")
    limiter = build_request_limiter(configured)

    async with AsyncExitStack() as stack:
        for _ in range(16):
            await stack.enter_async_context(limiter.acquire("official-smoke-key"))

        with pytest.raises(HTTPException) as failure:
            async with limiter.acquire("official-smoke-key"):
                pass

    assert failure.value.status_code == 429


@pytest.mark.asyncio
async def test_default_app_limiter_does_not_throttle_one_hundred_request_smoke_window() -> None:
    """官网同一阶段的 100 条 Smoke 请求不能被旧的 60/分钟上限截断。"""
    configured = Settings(database_url="postgresql+psycopg://unused/masm")
    limiter = build_request_limiter(configured)

    for _ in range(100):
        async with limiter.acquire("official-smoke-key"):
            pass


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
async def test_window_rate_limit_rejects() -> None:
    """窗口内请求数超过上限时被拒绝。"""
    timer = _FakeTimer()
    limiter = RequestLimiter(
        max_requests_per_window=2, window_seconds=60.0, retry_after_seconds=3, clock=timer
    )
    async with limiter.acquire("key-1"):
        pass
    async with limiter.acquire("key-1"):
        pass
    with pytest.raises(HTTPException) as excinfo:
        async with limiter.acquire("key-1"):
            pass
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "3"


@pytest.mark.asyncio
async def test_window_recovers_after_expiry() -> None:
    """窗口结束后恢复。"""
    timer = _FakeTimer()
    limiter = RequestLimiter(max_requests_per_window=1, window_seconds=60.0, clock=timer)
    async with limiter.acquire("key-1"):
        pass
    with pytest.raises(HTTPException):
        async with limiter.acquire("key-1"):
            pass
    timer.advance(61.0)
    async with limiter.acquire("key-1"):
        pass


@pytest.mark.asyncio
async def test_different_keys_are_independent() -> None:
    """不同 API Key 的配额互相独立。"""
    limiter = RequestLimiter(max_requests_per_window=1, window_seconds=60.0)
    async with limiter.acquire("key-a"):
        pass
    with pytest.raises(HTTPException):
        async with limiter.acquire("key-a"):
            pass
    async with limiter.acquire("key-b"):
        pass


@pytest.mark.asyncio
async def test_slot_released_on_exception() -> None:
    """上下文内异常退出后并发槽位被释放。"""
    limiter = RequestLimiter(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with limiter.acquire("key-1"):
            raise RuntimeError("boom")
    async with limiter.acquire("key-1"):
        pass


def test_limiter_state_uses_hashed_identifier(
    settings, database: Database, asset_store: AssetStore
) -> None:
    """限流状态只保存凭证哈希，不保存原始密钥。"""
    limiter = RequestLimiter()
    app = create_app(settings, database=database, asset_store=asset_store, limiter=limiter)
    with TestClient(app) as client:
        response = client.post(
            "/add", json=_payload(f"u-{uuid4().hex}"), headers={"X-Api-Key": "test-key"}
        )
    assert response.status_code == 200
    assert "test-key" not in limiter._history  # noqa: SLF001
    assert credential_fingerprint("test-key") in limiter._history  # noqa: SLF001


@pytest.mark.asyncio
async def test_add_returns_429_with_retry_after(
    settings, database: Database, asset_store: AssetStore
) -> None:
    """并发槽位被占用时 /add 返回 429 且带 Retry-After。"""
    limiter = RequestLimiter(max_concurrent=1, retry_after_seconds=5)
    app = create_app(settings, database=database, asset_store=asset_store, limiter=limiter)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with limiter.acquire(credential_fingerprint("test-key")):
            response = await client.post(
                "/add", json=_payload(f"u-{uuid4().hex}"), headers={"X-Api-Key": "test-key"}
            )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
