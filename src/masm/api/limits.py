"""请求并发/速率限制。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException


class RequestLimiter:
    """按 API Key 的并发限制器（内存实现，基线用）。"""

    def __init__(self, max_concurrent: int = 10, retry_after_seconds: int = 1) -> None:
        self._max_concurrent = max_concurrent
        self._retry_after_seconds = retry_after_seconds
        self._active: dict[str, int] = {}

    @asynccontextmanager
    async def acquire(self, api_key_id: str) -> AsyncIterator[None]:
        """获取一个并发槽位；超过上限时抛出 429 并携带 Retry-After。"""
        if self._active.get(api_key_id, 0) >= self._max_concurrent:
            raise HTTPException(
                status_code=429,
                detail="达到并发限制",
                headers={"Retry-After": str(self._retry_after_seconds)},
            )
        self._active[api_key_id] = self._active.get(api_key_id, 0) + 1
        try:
            yield
        finally:
            self._active[api_key_id] -= 1
