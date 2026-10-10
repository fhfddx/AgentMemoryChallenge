"""只含固定聚合字段的 Search 诊断；不记录请求、身份或证据正文。"""

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

_LOGGER = logging.getLogger("masm.search")
_CHANNELS = frozenset({"lexical", "text_vector", "image_vector", "metadata"})
_PROFILES = frozenset({"local-fake", "official-baseline", "official-masm"})
# selector 证据状态是固定枚举；任何其他取值都折叠为 unknown，绝不透传可控文本。
_EVIDENCE_STATES = frozenset({"sufficient", "partial", "insufficient", "unknown"})
_TAG_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class SearchDiagnostics:
    """无载荷、无用户标识的请求级计数与耗时。"""

    request_tag: str
    runtime_profile: str
    candidate_count: int
    dedup_count: int
    returned_count: int
    response_bytes: int
    latency_ms: float
    status_code: int
    channel_counts: Mapping[str, int]
    selector_candidate_count: int = 0
    selector_selected_count: int = 0
    selector_source_count: int = 0
    selector_selected_source_count: int = 0
    selector_fallback: bool = False
    selector_abstained: bool = False
    selector_failure_category: str = "none"
    selector_evidence_state: str = "unknown"
    selector_latency_ms: float = 0.0


def emit_search_diagnostics(value: SearchDiagnostics) -> None:
    """逐字段白名单序列化，拒绝将可控文本混入日志。"""
    payload = {
        "request_tag": (
            value.request_tag if _TAG_PATTERN.fullmatch(value.request_tag) else uuid4().hex
        ),
        "runtime_profile": (
            value.runtime_profile if value.runtime_profile in _PROFILES else "unknown"
        ),
        "candidate_count": max(0, int(value.candidate_count)),
        "dedup_count": max(0, int(value.dedup_count)),
        "returned_count": max(0, int(value.returned_count)),
        "response_bytes": max(0, int(value.response_bytes)),
        "latency_ms": round(max(0.0, float(value.latency_ms)), 2),
        "status_code": int(value.status_code),
        "channel_counts": {
            name: max(0, int(value.channel_counts[name]))
            for name in sorted(_CHANNELS & value.channel_counts.keys())
        },
        "selector_candidate_count": max(0, int(value.selector_candidate_count)),
        "selector_selected_count": max(0, int(value.selector_selected_count)),
        "selector_source_count": max(0, int(value.selector_source_count)),
        "selector_selected_source_count": max(0, int(value.selector_selected_source_count)),
        "selector_fallback": bool(value.selector_fallback),
        "selector_abstained": bool(value.selector_abstained),
        "selector_failure_category": (
            value.selector_failure_category
            if value.selector_failure_category in {"none", "unavailable", "invalid_output"}
            else "unknown"
        ),
        "selector_evidence_state": (
            value.selector_evidence_state
            if value.selector_evidence_state in _EVIDENCE_STATES
            else "unknown"
        ),
        "selector_latency_ms": round(max(0.0, float(value.selector_latency_ms)), 2),
    }
    _LOGGER.info(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        extra={"event": "search.completed", "extra": payload},
    )
