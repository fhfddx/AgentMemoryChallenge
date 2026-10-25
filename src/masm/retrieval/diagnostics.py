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
# 证据图只记录固定枚举与计数；失败类别同样只允许固定取值。
_GRAPH_STATES = _EVIDENCE_STATES
_GRAPH_FAILURES = frozenset(
    {"none", "planner_unavailable", "planner_invalid", "internal_error"}
)
# 结构失败原因同样是固定枚举：任何其他取值折叠为 unknown。
_GRAPH_PLANNER_FAILURE_REASONS = frozenset(
    {
        "none",
        "schema_invalid",
        "planner_unavailable",
        "internal_error",
        "candidate_scope_mismatch",
        "empty_requirements",
        "requirement_count_exceeded",
        "requirement_quote_not_grounded",
        "requirement_quote_reordered",
        "requirement_quote_paraphrased",
        "support_count_exceeded",
        "support_index_out_of_range",
        "link_count_exceeded",
        "link_index_out_of_range",
        "link_self_reference",
    }
)
_TAG_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
# 桥接（R11）未命中的固定原因枚举：用于定位"为什么没补上第二会话"，不含任何文本。
_GRAPH_BRIDGE_SKIPS = frozenset(
    {
        "none",
        "no_selected",
        "no_question_unit",
        "no_selected_unit",
        "no_rare_link",
        "no_question_overlap",
        "no_cross_session_candidate",
    }
)


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
    graph_enabled: bool = False
    graph_planner_calls: int = 0
    graph_candidate_count: int = 0
    graph_requirement_count: int = 0
    graph_support_count: int = 0
    graph_link_count: int = 0
    graph_component_count: int = 0
    graph_selected_count: int = 0
    graph_selected_source_count: int = 0
    graph_state: str = "unknown"
    graph_fallback: bool = False
    graph_failure_category: str = "none"
    graph_planner_failure_reason: str = "none"
    graph_vetoed: bool = False
    graph_bridge_added: int = 0
    graph_bridge_skip: str = "none"
    graph_planner_latency_ms: float = 0.0
    # 选择器之前的拒答预检是否命中：用于区分"预检拒答"与"选择器弃答"。
    refusal_gate: bool = False
    # 空结果补救补进来多少条候选（0 表示没有触发或没补到东西）。
    retry_added: int = 0
    # 池子够大但仍判"证据不足"时，按 best-effort 返回了多少条证据。
    best_effort: int = 0


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
        "graph_enabled": bool(value.graph_enabled),
        "graph_planner_calls": max(0, int(value.graph_planner_calls)),
        "graph_candidate_count": max(0, int(value.graph_candidate_count)),
        "graph_requirement_count": max(0, int(value.graph_requirement_count)),
        "graph_support_count": max(0, int(value.graph_support_count)),
        "graph_link_count": max(0, int(value.graph_link_count)),
        "graph_component_count": max(0, int(value.graph_component_count)),
        "graph_selected_count": max(0, int(value.graph_selected_count)),
        "graph_selected_source_count": max(0, int(value.graph_selected_source_count)),
        "graph_state": (
            value.graph_state if value.graph_state in _GRAPH_STATES else "unknown"
        ),
        "graph_fallback": bool(value.graph_fallback),
        "graph_failure_category": (
            value.graph_failure_category
            if value.graph_failure_category in _GRAPH_FAILURES
            else "unknown"
        ),
        "graph_planner_failure_reason": (
            value.graph_planner_failure_reason
            if value.graph_planner_failure_reason in _GRAPH_PLANNER_FAILURE_REASONS
            else "unknown"
        ),
        "graph_vetoed": bool(value.graph_vetoed),
        "graph_bridge_added": max(0, int(value.graph_bridge_added)),
        "graph_bridge_skip": (
            value.graph_bridge_skip
            if value.graph_bridge_skip in _GRAPH_BRIDGE_SKIPS
            else "unknown"
        ),
        "graph_planner_latency_ms": round(max(0.0, float(value.graph_planner_latency_ms)), 2),
        "refusal_gate": bool(value.refusal_gate),
        "retry_added": max(0, int(value.retry_added)),
        "best_effort": max(0, int(value.best_effort)),
    }
    _LOGGER.info(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        extra={"event": "search.completed", "extra": payload},
    )
