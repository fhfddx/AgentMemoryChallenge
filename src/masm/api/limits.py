"""请求并发与速率限制（内存实现，时间源可注入）。

状态只以「API Key 的哈希标识」为键，绝不保存原始密钥。
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import HTTPException


class RequestLimiter:
    """按密钥哈希标识的并发 + 时间窗口速率限制器。"""

    def __init__(
        self,
        *,
        max_concurrent: int = 10,
        max_requests_per_window: int = 60,
        window_seconds: float = 60.0,
        retry_after_seconds: int = 1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_requests_per_window = max_requests_per_window
        self._window_seconds = window_seconds
        self._retry_after_seconds = retry_after_seconds
        self._clock = clock or time.monotonic
        self._active: dict[str, int] = {}
        self._history: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, api_key_id: str) -> AsyncIterator[None]:
        """获取并发槽位；超过并发或窗口速率上限时抛出 429。"""
        async with self._lock:
            now = self._clock()
            history = self._history.setdefault(api_key_id, deque())
            self._prune(history, now)
            if len(history) >= self._max_requests_per_window:
                raise self._reject("达到时间窗口速率限制")
            if self._active.get(api_key_id, 0) >= self._max_concurrent:
                raise self._reject("达到并发限制")
            history.append(now)
            self._active[api_key_id] = self._active.get(api_key_id, 0) + 1
        try:
            yield
        finally:
            async with self._lock:
                self._active[api_key_id] -= 1

    def _prune(self, history: deque[float], now: float) -> None:
        """丢弃窗口外的历史记录。"""
        boundary = now - self._window_seconds
        while history and history[0] <= boundary:
            history.popleft()

    def _reject(self, detail: str) -> HTTPException:
        return HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(self._retry_after_seconds)},
        )
