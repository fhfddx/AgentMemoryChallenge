"""模型服务不得阻塞 FastAPI 事件循环。"""

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

from masm.api.limits import RequestLimiter
from masm.api.routes import router
from masm.config import Settings
from masm.schemas.api import SearchResponse


class _SlowSearchService:
    def search(self, request) -> SearchResponse:
        time.sleep(0.3)
        return SearchResponse(data=[])


@pytest.mark.asyncio
async def test_slow_model_search_does_not_delay_health_check() -> None:
    """同步 Provider 在工作线程执行时，浅健康检查仍能及时响应。"""
    app = FastAPI()
    app.state.settings = Settings(
        database_url="postgresql+psycopg://postgres@localhost/masm",
        api_keys=("test-key",),
    )
    app.state.limiter = RequestLimiter()
    app.state.search_service = _SlowSearchService()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        search_task = asyncio.create_task(
            client.post(
                "/search",
                headers={"X-Api-Key": "test-key"},
                json={"query": "slow query", "user_id": "user-1", "top_k": 1},
            )
        )

        async def health_after_search_starts() -> tuple[httpx.Response, float]:
            await asyncio.sleep(0.02)
            response = await client.get("/health")
            return response, time.perf_counter() - started

        health_response, health_elapsed = await health_after_search_starts()
        search_response = await search_task

    assert health_response.status_code == 200
    assert health_elapsed < 0.15
    assert search_response.status_code == 200
