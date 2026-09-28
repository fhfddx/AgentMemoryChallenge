"""聚合指标：只记录数量、延迟、状态、模型版本、token、成本与降级等元数据。"""

from collections import Counter
from dataclasses import dataclass, field
from threading import Lock

# 指标允许的维度键（白名单）。
ALLOWED_METRIC_KEYS = frozenset(
    {
        "endpoint",
        "status",
        "status_code",
        "channel",
        "model_name",
        "model_version",
        "prompt_version",
        "agent_name",
        "degraded",
    }
)


def _validate_dimensions(dimensions: dict[str, object]) -> dict[str, str]:
    unknown = set(dimensions) - ALLOWED_METRIC_KEYS
    if unknown:
        raise ValueError(f"不允许的指标维度: {sorted(unknown)}")
    return {key: str(value) for key, value in dimensions.items()}


@dataclass
class MetricsRegistry:
    """线程安全的进程内聚合指标。"""

    counters: Counter = field(default_factory=Counter)
    latency_ms_total: dict[str, float] = field(default_factory=dict)
    latency_ms_max: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def increment(self, name: str, value: int = 1, **dimensions: object) -> None:
        """累加一个计数指标。"""
        key = _key(name, _validate_dimensions(dimensions))
        with self._lock:
            self.counters[key] += value

    def observe_latency(self, name: str, latency_ms: float, **dimensions: object) -> None:
        """记录一次延迟（聚合为总和与最大值）。"""
        key = _key(name, _validate_dimensions(dimensions))
        with self._lock:
            self.counters[key] += 1
            self.latency_ms_total[key] = self.latency_ms_total.get(key, 0.0) + float(latency_ms)
            self.latency_ms_max[key] = max(self.latency_ms_max.get(key, 0.0), float(latency_ms))

    def record_request(self, *, endpoint: str, status: str, latency_ms: float) -> None:
        """记录一次 HTTP 请求的聚合结果。"""
        self.increment("http_requests", endpoint=endpoint, status=status)
        self.observe_latency("http_latency_ms", latency_ms, endpoint=endpoint)

    def record_model_call(
        self,
        *,
        model_name: str,
        prompt_version: str,
        latency_ms: float,
        tokens: int = 0,
        cost_usd: float = 0.0,
        degraded: bool = False,
    ) -> None:
        """记录一次模型调用与降级标记。"""
        self.increment(
            "model_calls",
            model_name=model_name,
            prompt_version=prompt_version,
            degraded=degraded,
        )
        self.increment("model_tokens", tokens, model_name=model_name)
        self.observe_latency("model_latency_ms", latency_ms, model_name=model_name)
        if cost_usd:
            self.increment("model_cost_micro_usd", int(cost_usd * 1_000_000), model_name=model_name)

    def snapshot(self) -> dict[str, object]:
        """返回只含聚合元数据的快照。"""
        with self._lock:
            counters = dict(self.counters)
            totals = dict(self.latency_ms_total)
            maxima = dict(self.latency_ms_max)
        return {
            "counters": counters,
            "latency_ms_total": totals,
            "latency_ms_max": maxima,
            "latency_ms_avg": {
                key: (totals[key] / counters[key]) if counters.get(key) else 0.0 for key in totals
            },
        }


def _key(name: str, dimensions: dict[str, str]) -> str:
    if not dimensions:
        return name
    parts = ",".join(f"{key}={dimensions[key]}" for key in sorted(dimensions))
    return f"{name}{{{parts}}}"
