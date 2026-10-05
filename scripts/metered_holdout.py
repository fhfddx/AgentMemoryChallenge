"""仅供本机评估使用的留出集与 Provider 用量计量。

计量器只保留数字、模型名和状态，不保存请求、响应正文或认证头。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image
from scripts.evaluate_synthetic_recall import (
    EvaluationCase,
    aggregate_results,
    score_search_case,
)
from scripts.evaluate_synthetic_recall import build_cases as build_synthetic_cases
from sqlalchemy.engine import make_url

ProviderKind = Literal["llm", "embedding"]
_USAGE_KEYS = frozenset({
    "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
})
_PRICE_KEYS = frozenset({"llm_input_usd", "llm_output_usd", "embedding_input_cny"})
_CURRENT_STAGE: ContextVar[str] = ContextVar("metered_holdout_stage", default="unattributed")


@dataclass(frozen=True)
class StageRecord:
    """固定阶段名与耗时；不含参数、返回值或异常正文。"""

    stage: str
    latency_ms: float
    succeeded: bool


@dataclass(frozen=True)
class AttemptFailureRecord:
    """Provider 单次失败尝试的白名单分类；不保留异常对象或消息。"""

    provider: ProviderKind
    stage: str
    kind: Literal["http_status", "timeout", "transport", "response_validation"]
    status_code: int | None


@contextmanager
def meter_app_stages(app: Any) -> Any:
    """仅在本机留出集执行期间包装既有 Add/Search 方法并在退出时还原。"""
    records: list[StageRecord] = []
    service = app.state.add_service
    pipeline = service._pipeline

    def instrument(owner: Any, method_name: str, stage: str, stack: ExitStack) -> None:
        original = getattr(owner, method_name)

        def measured(*args: Any, **kwargs: Any) -> Any:
            token = _CURRENT_STAGE.set(stage)
            started = perf_counter()
            succeeded = False
            try:
                result = original(*args, **kwargs)
                succeeded = True
                return result
            finally:
                records.append(StageRecord(stage, (perf_counter() - started) * 1000, succeeded))
                _CURRENT_STAGE.reset(token)

        stack.enter_context(patch.object(owner, method_name, measured))

    with ExitStack() as stack:
        targets = [(app.state.search_service, "search", "search")]
        if pipeline is not None:
            targets = [
                (pipeline._perception, "extract", "perception"),
                (pipeline, "_recall", "recall"),
                (pipeline._temporal, "analyze", "temporal"),
                (pipeline._curator, "propose", "curator"),
                (service, "_prepare", "embedding_prepare"),
                (service._repo, "finalize_request", "commit"),
                *targets,
            ]
            fused = getattr(pipeline, "_fused_text", None)
            if fused is not None:
                targets.insert(0, (fused, "extract", "fused_text"))
        for owner, method_name, stage in targets:
            instrument(owner, method_name, stage, stack)
        yield records


def summarize_stage_latency(records: Sequence[StageRecord]) -> dict[str, dict[str, int | float]]:
    """只输出固定阶段的聚合耗时与失败数。"""
    grouped: dict[str, list[StageRecord]] = {}
    for row in records:
        grouped.setdefault(row.stage, []).append(row)
    return {
        stage: {
            "count": len(rows),
            "mean_ms": round(statistics.mean(row.latency_ms for row in rows), 2),
            "max_ms": round(max(row.latency_ms for row in rows), 2),
            "failures": sum(not row.succeeded for row in rows),
        }
        for stage, rows in sorted(grouped.items())
    }


def summarize_attempt_failures(
    meters: Sequence[MeteredClient],
) -> list[dict[str, str | int | None]]:
    """只按 Provider、阶段、错误类别和状态码输出失败次数。"""
    counts: dict[tuple[str, str, str, int | None], int] = {}
    for meter in meters:
        for row in meter.attempt_failures:
            key = (row.provider, row.stage, row.kind, row.status_code)
            counts[key] = counts.get(key, 0) + 1
    return [
        {"provider": provider, "stage": stage, "kind": kind,
         "status_code": status_code, "count": count}
        for (provider, stage, kind, status_code), count in sorted(
            counts.items(), key=lambda item: (*item[0][:3], item[0][3] or 0),
        )
    ]


def build_diagnostic_summary(
    stages: Sequence[StageRecord], meters: Sequence[MeteredClient],
) -> dict[str, Any]:
    """为新报告生成不含单次请求信息的阶段与失败聚合。"""
    return {
        "stage_latency_ms": summarize_stage_latency(stages),
        "attempt_failures": summarize_attempt_failures(meters),
    }


@dataclass(frozen=True)
class UsageRecord:
    """一次实际 HTTP 尝试的非敏感计量元数据。"""

    provider: ProviderKind
    model: str
    status_code: int
    input_count: int
    usage: dict[str, int] | None
    image_blocks: int = 0


def _numeric_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage = {
        key: item for key, item in value.items()
        if key in _USAGE_KEYS and isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    return usage or None


def _llm_image_blocks(body: Any) -> int:
    """只数 LLM 内容块类型，不读取或保存图片 URL/字节。"""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return 0
    count = 0
    for message in body["messages"]:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        count += sum(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message["content"]
        )
    return count


class MeteredClient:
    """包装现有同步 HTTP Client；透传响应，仅截取受控 usage 数值。"""

    def __init__(
        self, upstream: httpx.Client, *, provider: ProviderKind, model: str,
        attempt_limit: int | None = None,
    ) -> None:
        if attempt_limit is not None and attempt_limit < 1:
            raise ValueError("Provider HTTP 尝试上限必须为正整数")
        self._upstream = upstream
        self._provider = provider
        self._model = model
        self._attempt_limit = attempt_limit
        self._attempts_started = 0
        self._attempt_limit_exceeded = False
        self._attempt_lock = Lock()
        self.records: list[UsageRecord] = []
        self.attempt_failures: list[AttemptFailureRecord] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        with self._attempt_lock:
            if self._attempt_limit is not None and self._attempts_started >= self._attempt_limit:
                self._attempt_limit_exceeded = True
                raise RuntimeError("本机评估 Provider HTTP 尝试次数达到上限")
            self._attempts_started += 1
        body = kwargs.get("json")
        inputs = body.get("input") if isinstance(body, dict) else None
        input_count = (
            len(inputs) if self._provider == "embedding" and isinstance(inputs, list) else 0
        )
        image_blocks = _llm_image_blocks(body) if self._provider == "llm" else 0
        try:
            response = self._upstream.post(url, **kwargs)
        except httpx.HTTPError:
            self.records.append(UsageRecord(
                self._provider, self._model, 0, input_count, None, image_blocks,
            ))
            raise
        try:
            data = response.json()
        except ValueError:
            data = None
        usage = _numeric_usage(data.get("usage")) if isinstance(data, dict) else None
        self.records.append(UsageRecord(
            self._provider, self._model, response.status_code, input_count, usage, image_blocks,
        ))
        return response

    @property
    def attempt_limit_exceeded(self) -> bool:
        """是否已有请求被硬停止线拒绝；供评估边界识别被内部吞掉的异常。"""
        with self._attempt_lock:
            return self._attempt_limit_exceeded


def raise_if_attempt_limit_exceeded(meters: Sequence[MeteredClient]) -> None:
    """降级路径可吞 Provider 异常，但本机评估不能接受越线后的结果。"""
    if any(meter.attempt_limit_exceeded for meter in meters):
        raise RuntimeError("本机评估 Provider HTTP 尝试次数达到上限")


def _input_tokens(usage: Mapping[str, int]) -> int | None:
    return usage.get("prompt_tokens", usage.get("input_tokens"))


def _output_tokens(usage: Mapping[str, int]) -> int | None:
    return usage.get("completion_tokens", usage.get("output_tokens"))


def summarize_usage(
    records: Sequence[UsageRecord],
    *,
    prices_per_million: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """聚合调用和用量；不完整记录不推断费用。"""
    llm_calls = sum(row.provider == "llm" for row in records)
    embedding_calls = sum(row.provider == "embedding" for row in records)
    usage_missing = 0
    llm_input = llm_output = embedding_input = 0
    for row in records:
        usage = row.usage or {}
        input_tokens = _input_tokens(usage)
        output_tokens = _output_tokens(usage)
        if row.provider == "llm":
            if input_tokens is None or output_tokens is None:
                usage_missing += 1
            else:
                llm_input += input_tokens
                llm_output += output_tokens
        elif input_tokens is None:
            usage_missing += 1
        else:
            embedding_input += input_tokens

    estimated_cost: str | dict[str, str] = "unavailable"
    if (
        usage_missing == 0
        and prices_per_million is not None
        and _PRICE_KEYS <= prices_per_million.keys()
        and all(prices_per_million[key] >= 0 for key in _PRICE_KEYS)
    ):
        llm_cost = (
            Decimal(llm_input) * prices_per_million["llm_input_usd"]
            + Decimal(llm_output) * prices_per_million["llm_output_usd"]
        ) / Decimal(1_000_000)
        embedding_cost = (
            Decimal(embedding_input) * prices_per_million["embedding_input_cny"]
        ) / Decimal(1_000_000)
        estimated_cost = {
            "llm_usd": format(llm_cost.normalize(), "f"),
            "embedding_cny": format(embedding_cost.normalize(), "f"),
            "combined": "unavailable",
        }
    return {
        "llm_calls": llm_calls,
        "llm_image_attempts": sum(
            row.provider == "llm" and row.image_blocks > 0 for row in records
        ),
        "llm_image_blocks": sum(
            row.image_blocks for row in records if row.provider == "llm"
        ),
        "image_usage_missing": sum(
            row.provider == "llm" and row.image_blocks > 0
            and (_input_tokens(row.usage or {}) is None
                 or _output_tokens(row.usage or {}) is None)
            for row in records
        ),
        "embedding_calls": embedding_calls,
        "embedding_inputs": sum(row.input_count for row in records if row.provider == "embedding"),
        "http_failures": sum(not 200 <= row.status_code < 300 for row in records),
        "usage_missing": usage_missing,
        "token_usage": {
            "llm_input": llm_input, "llm_output": llm_output,
            "embedding_input": embedding_input,
        },
        "estimated_cost": estimated_cost,
    }


def build_holdout_cases(
    run_tag: str, *, case_set: str = "primary",
) -> list[EvaluationCase]:
    """构造指定的六类合成留出样本；查询不直接包含答案标记。"""
    if case_set == "alternate-v1":
        return _build_alternate_holdout_cases(run_tag)
    if case_set == "media-v1":
        source = {case.category: case for case in build_synthetic_cases(run_tag)}
        return [source["text_image"], source["image_only"]]
    if case_set != "primary":
        raise ValueError(f"未知留出集：{case_set}")

    notebook = f"Larch-{run_tag}"
    venue = f"Cedar-{run_tag}"
    project, review = f"Zephyr-{run_tag}", f"Apricot-{run_tag}"
    lead, backup = f"Birch-{run_tag}", f"Pine-{run_tag}"
    cabinet = f"Moss-{run_tag}"
    return [
        EvaluationCase(
            "preference", (({"role": "user", "content":
                f"For field notes I prefer the {notebook} notebook."},),),
            "Which notebook does the user prefer for field notes?", (notebook,),
        ),
        EvaluationCase(
            "conditional_plan", (({"role": "user", "content":
                f"If the main hall closes, move the workshop to {venue}."},),),
            "Where should the workshop move if its usual hall is unavailable?", (venue,),
        ),
        EvaluationCase(
            "cross_session", (
                ({"role": "user", "content": f"My project is called {project}."},),
                ({"role": "user", "content": f"The review for that project is on {review}."},),
            ), "When is the review for the project discussed earlier?", (review,),
        ),
        EvaluationCase(
            "multi_fact", ((
                {"role": "user", "content": f"The primary contact is {lead}."},
                {"role": "user", "content": f"The backup contact is {backup}."},
            ),), "Who are the primary and backup contacts?", (lead, backup),
        ),
        EvaluationCase(
            "distractor", ((
                {"role": "user", "content": "The office printer is near the window."},
                {"role": "user", "content": f"The backup drive is stored in {cabinet}."},
                {"role": "user", "content": "The meeting room has three chairs."},
            ),), "Where is the backup drive stored?", (cabinet,),
        ),
        EvaluationCase(
            "abstention", (), "What is the user's passport number?", (), expect_empty=True,
        ),
    ]


def _build_alternate_holdout_cases(run_tag: str) -> list[EvaluationCase]:
    """语义和措辞不同、结构与主留出集一致的替代模板。"""
    folder = f"Saffron-{run_tag}"
    route = f"Harbor-{run_tag}"
    archive, pickup = f"Orion-{run_tag}", f"Juniper-{run_tag}"
    lead, alternate = f"Maple-{run_tag}", f"Willow-{run_tag}"
    envelope = f"Quartz-{run_tag}"
    return [
        EvaluationCase(
            "preference", (({"role": "user", "content":
                f"Use the {folder} folder for quarterly audit notes."},),),
            "Which folder should hold the user's quarterly audit notes?", (folder,),
        ),
        EvaluationCase(
            "conditional_plan", (({"role": "user", "content":
                f"When the north bridge is closed, route deliveries through {route}."},),),
            "Which route should deliveries use if the usual bridge is unavailable?", (route,),
        ),
        EvaluationCase(
            "cross_session", (
                ({"role": "user", "content": f"The archive box is catalogued as {archive}."},),
                ({"role": "user", "content":
                    f"Pickup for that archive box is scheduled on {pickup}."},),
            ), "When is pickup scheduled for the archive box mentioned earlier?", (pickup,),
        ),
        EvaluationCase(
            "multi_fact", ((
                {"role": "user", "content":
                    f"If the lead responder is unavailable, contact {alternate}."},
                {"role": "user", "content": f"The lead responder is {lead}."},
            ),), "Who are the lead and alternate responders?", (lead, alternate),
        ),
        EvaluationCase(
            "distractor", ((
                {"role": "user", "content": "The loading bay closes at six."},
                {"role": "user", "content":
                    f"The recovery-code envelope is stored in {envelope}."},
                {"role": "user", "content": "The lobby clock runs five minutes fast."},
            ),), "Where is the recovery-code envelope stored?", (envelope,),
        ),
        EvaluationCase(
            "abstention", (), "What is the user's national identification number?", (),
            expect_empty=True,
        ),
    ]


def load_external_cases(
    path: Path, *, expected_sha256: str,
) -> tuple[list[EvaluationCase], dict[str, Any]]:
    """读取预先锁定的小型文本样本，报告只保留来源元数据。"""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("外部样本 SHA-256 无效")
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError("外部样本无效：文件不存在或超过 128 KiB")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("外部样本 SHA-256 不匹配")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("外部样本无效：JSON 格式错误") from None
    try:
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("外部样本无效：包含无法写入报告的字符") from None
    if not isinstance(document, dict) or set(document) != {
        "source_name", "source_url", "selection_rule", "cases",
    }:
        raise ValueError("外部样本无效：顶层字段错误")
    source_name = document["source_name"]
    source_url = document["source_url"]
    selection_rule = document["selection_rule"]
    if not isinstance(source_name, str) or not 1 <= len(source_name) <= 80 or "\n" in source_name:
        raise ValueError("外部样本无效：来源名称错误")
    if (
        not isinstance(selection_rule, str)
        or not 1 <= len(selection_rule) <= 200
        or "\n" in selection_rule
    ):
        raise ValueError("外部样本无效：抽样规则错误")
    if not isinstance(source_url, str) or len(source_url) > 300:
        raise ValueError("外部样本无效：来源 URL 错误")
    try:
        url = urlsplit(source_url)
    except ValueError:
        raise ValueError("外部样本无效：来源 URL 错误") from None
    if (
        url.scheme != "https" or not url.hostname or url.username or url.password
        or url.query or url.fragment
    ):
        raise ValueError("外部样本无效：来源 URL 错误")
    host = url.hostname
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("外部样本无效：来源 URL 不是公开地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("外部样本无效：来源 URL 不是公开地址")
    rows = document["cases"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 6:
        raise ValueError("外部样本无效：查询数必须为 1 至 6")
    cases: list[EvaluationCase] = []
    item_ids: list[str] = []
    seen_categories: set[str] = set()
    total_sessions = total_messages = total_chars = 0
    for row in rows:
        required_fields = {"id", "category", "sessions", "query", "expected_markers"}
        if not isinstance(row, dict) or set(row) not in (
            required_fields, required_fields | {"expect_empty"},
        ):
            raise ValueError("外部样本无效：案例字段错误")
        if "expect_empty" in row and row["expect_empty"] is not True:
            raise ValueError("外部样本无效：拒答标志错误")
        expect_empty = row.get("expect_empty") is True
        item_id, category = row["id"], row["category"]
        if (
            not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item_id)
            or item_id in item_ids or not isinstance(category, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", category)
            or category in seen_categories
        ):
            raise ValueError("外部样本无效：ID 或类别错误")
        query, markers = row["query"], row["expected_markers"]
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ValueError("外部样本无效：查询错误")
        if (
            not isinstance(markers, list)
            or (len(markers) != 0 if expect_empty else not 1 <= len(markers) <= 4)
            or any(
                not isinstance(marker, str) or not 1 <= len(marker) <= 100
                or not marker.strip() for marker in markers
            )
            or len(set(markers)) != len(markers)
            or any(marker in query for marker in markers)
        ):
            raise ValueError("外部样本无效：答案标记错误")
        if any(
            marker in field
            for marker in markers
            for field in (source_name, source_url, selection_rule, item_id, category)
        ):
            raise ValueError("外部样本无效：答案标记出现在报告元数据中")
        sessions = row["sessions"]
        if not isinstance(sessions, list) or not 1 <= len(sessions) <= 3:
            raise ValueError("外部样本无效：会话数错误")
        additions: list[tuple[dict[str, str], ...]] = []
        contents: list[str] = []
        for session in sessions:
            if not isinstance(session, list) or not 1 <= len(session) <= 6:
                raise ValueError("外部样本无效：消息数错误")
            messages: list[dict[str, str]] = []
            for message in session:
                if (
                    not isinstance(message, dict) or set(message) != {"role", "content"}
                    or not isinstance(message["role"], str)
                    or message["role"] not in {"user", "assistant"}
                    or not isinstance(message["content"], str)
                    or not 1 <= len(message["content"].strip()) <= 2000
                ):
                    raise ValueError("外部样本无效：仅允许纯文本 user/assistant 消息")
                messages.append({"role": message["role"], "content": message["content"]})
                contents.append(message["content"])
                total_chars += len(message["content"])
            additions.append(tuple(messages))
            total_messages += len(messages)
            total_sessions += 1
        if any(not any(marker in content for content in contents) for marker in markers):
            raise ValueError("外部样本无效：答案标记不在来源消息中")
        item_ids.append(item_id)
        seen_categories.add(category)
        cases.append(EvaluationCase(
            category, tuple(additions), query, tuple(markers), expect_empty=expect_empty,
        ))
    if total_sessions > 8 or total_messages > 24 or total_chars > 12_000:
        raise ValueError("外部样本无效：总 Add 或正文规模超过上限")
    return cases, {
        "source_name": source_name,
        "source_url": source_url,
        "selection_rule": selection_rule,
        "source_file_sha256": digest,
        "source_item_ids": item_ids,
    }


def load_external_media_cases(
    path: Path, *, expected_sha256: str,
) -> tuple[list[EvaluationCase], dict[str, Any]]:
    """读取逐图锁定的 CC0 小样本，报告不保存图片、查询或答案。"""
    error = "外部媒体样本无效"
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError(f"{error}：样本 SHA-256 错误")
    try:
        if path.is_symlink():
            raise ValueError(f"{error}：样本文件不能是符号链接")
        path = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{error}：文件不存在或超过 128 KiB") from None
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError(f"{error}：文件不存在或超过 128 KiB")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError(f"{error}：样本 SHA-256 不匹配")
    try:
        document = json.loads(raw)
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{error}：JSON 格式错误") from None
    if not isinstance(document, dict) or set(document) != {
        "source_name", "selection_rule", "cases",
    }:
        raise ValueError(f"{error}：顶层字段错误")
    source_name = document["source_name"]
    selection_rule = document["selection_rule"]
    if (
        not isinstance(source_name, str) or not 1 <= len(source_name) <= 80
        or "\n" in source_name
    ):
        raise ValueError(f"{error}：来源名称错误")
    if (
        not isinstance(selection_rule, str)
        or not 1 <= len(selection_rule) <= 200 or "\n" in selection_rule
    ):
        raise ValueError(f"{error}：抽样规则错误")
    rows = document["cases"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError(f"{error}：查询数必须为 1 至 4")

    neutral_text = "Independent CC0 reference image; remember only the visible content."
    allowed_formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    cases: list[EvaluationCase] = []
    item_ids: list[str] = []
    seen_categories: set[str] = set()
    media_audit: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "id", "category", "source_url", "license", "image_file",
            "image_sha256", "query", "expected_markers",
        }:
            raise ValueError(f"{error}：案例字段错误")
        item_id, category = row["id"], row["category"]
        if (
            not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item_id)
            or item_id in item_ids or not isinstance(category, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", category)
            or category in seen_categories
        ):
            raise ValueError(f"{error}：ID 或类别错误")
        source_url = row["source_url"]
        if not isinstance(source_url, str) or len(source_url) > 300:
            raise ValueError(f"{error}：来源 URL 错误")
        try:
            url = urlsplit(source_url)
        except ValueError:
            raise ValueError(f"{error}：来源 URL 错误") from None
        if (
            url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment
        ):
            raise ValueError(f"{error}：来源 URL 错误")
        host = url.hostname
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError(f"{error}：来源 URL 不是公开地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError(f"{error}：来源 URL 不是公开地址")
        if row["license"] != "CC0-1.0":
            raise ValueError(f"{error}：仅允许 CC0-1.0 图片")
        image_file, image_digest = row["image_file"], row["image_sha256"]
        if (
            not isinstance(image_file, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", image_file)
            or Path(image_file).name != image_file
            or not isinstance(image_digest, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", image_digest)
        ):
            raise ValueError(f"{error}：图片引用错误")
        manifest_dir = path.parent.resolve(strict=True)
        image_path = manifest_dir / image_file
        if image_path.is_symlink():
            raise ValueError(f"{error}：图片不能是符号链接")
        try:
            image_path = image_path.resolve(strict=True)
        except OSError:
            raise ValueError(f"{error}：图片不存在或超过 10 MiB") from None
        if not image_path.is_relative_to(manifest_dir):
            raise ValueError(f"{error}：图片必须位于样本目录内")
        if not image_path.is_file() or not 1 <= image_path.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError(f"{error}：图片不存在或超过 10 MiB")
        image_bytes = image_path.read_bytes()
        actual_image_digest = hashlib.sha256(image_bytes).hexdigest()
        if actual_image_digest != image_digest.lower():
            raise ValueError(f"{error}：图片 SHA-256 不匹配")
        try:
            with Image.open(image_path) as image:
                image_format = image.format
                image.verify()
        except (OSError, Image.DecompressionBombError):
            raise ValueError(f"{error}：图片格式错误") from None
        mime_type = allowed_formats.get(image_format or "")
        if mime_type is None:
            raise ValueError(f"{error}：图片格式错误")
        query, markers = row["query"], row["expected_markers"]
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ValueError(f"{error}：查询错误")
        if (
            not isinstance(markers, list) or not 1 <= len(markers) <= 4
            or any(
                not isinstance(marker, str) or not 1 <= len(marker) <= 100
                or marker != marker.strip()
                for marker in markers
            )
            or len(set(markers)) != len(markers)
            or any(marker.casefold() in query.casefold() for marker in markers)
        ):
            raise ValueError(f"{error}：答案标记错误")
        audit_fields = (
            source_name, selection_rule, item_id, category, source_url,
            row["license"], image_digest,
        )
        if any(
            marker.casefold() in field.casefold()
            for marker in markers for field in audit_fields
        ):
            raise ValueError(f"{error}：答案标记出现在报告元数据中")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content = [
            {"type": "text", "text": neutral_text},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime_type};base64,{encoded}",
            }},
        ]
        item_ids.append(item_id)
        seen_categories.add(category)
        cases.append(EvaluationCase(
            category, (({"role": "user", "content": content},),),
            query, tuple(markers),
        ))
        media_audit.append({
            "id": item_id,
            "source_url": source_url,
            "license": row["license"],
            "image_sha256": actual_image_digest,
        })
    return cases, {
        "source_name": source_name,
        "selection_rule": selection_rule,
        "source_file_sha256": digest,
        "source_item_ids": item_ids,
        "media": media_audit,
    }


def evaluate_client(
    client: Any,
    cases: Sequence[EvaluationCase],
    *,
    version: str,
    run_tag: str,
    api_key: str,
    register_cleanup: Any,
    attempt_meters: Sequence[MeteredClient] = (),
) -> dict[str, Any]:
    """经真实 API 路由跑小样本，只返回脱敏聚合指标。"""
    raise_if_attempt_limit_exceeded(attempt_meters)
    health = client.get("/health")
    if health.status_code != 200 or health.json() != {"status": "ok"}:
        raise RuntimeError("Health 检查失败")
    scores: dict[str, list[dict[str, Any]]] = {}
    add_latencies: list[float] = []
    search_latencies: list[float] = []
    headers = {"X-Api-Key": api_key}
    for case in cases:
        user_id = f"synthetic-holdout-{run_tag}-{version}-{case.category}"
        for index, messages in enumerate(case.additions):
            run_id = f"{user_id}-{index}"
            register_cleanup((user_id, run_id))
            started = perf_counter()
            response = client.post("/add", headers=headers, json={
                "request_id": run_id,
                "user_id": user_id,
                "session_id": f"synthetic-holdout-{run_tag}-{case.category}-session-{index}",
                "messages": list(messages),
            })
            add_latencies.append((perf_counter() - started) * 1000)
            raise_if_attempt_limit_exceeded(attempt_meters)
            if response.status_code != 200:
                raise RuntimeError(f"Add HTTP {response.status_code}")
            if response.json().get("success") is not True:
                raise RuntimeError("Add 契约错误")
        started = perf_counter()
        response = client.post("/search", headers=headers, json={
            "query": case.query, "user_id": user_id, "top_k": case.top_k,
        })
        search_latencies.append((perf_counter() - started) * 1000)
        raise_if_attempt_limit_exceeded(attempt_meters)
        if response.status_code != 200:
            raise RuntimeError(f"Search HTTP {response.status_code}")
        data = response.json().get("data")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise RuntimeError("Search 契约错误")
        if len(data) > case.top_k or len(response.content) > 30 * 1024 * 1024:
            raise RuntimeError("Search 返回超限")
        scores.setdefault(case.category, []).append(
            score_search_case(case, data, response_bytes=len(response.content))
        )
    raise_if_attempt_limit_exceeded(attempt_meters)
    return aggregate_results(
        scores, add_latencies=add_latencies, search_latencies=search_latencies,
    )


def run_app_holdout(
    app: Any,
    cases: Sequence[EvaluationCase],
    *,
    version: str,
    run_tag: str,
    api_key: str,
    cleanup_output: Path,
    cleanup_run: Callable[[str, str], bool] | None = None,
    attempt_meters: Sequence[MeteredClient] = (),
) -> dict[str, Any]:
    """在本机应用内运行评估，失败时也清理精确 Add 运行。"""
    from fastapi.testclient import TestClient

    if cleanup_run is None:
        from masm.services.deletion_service import DeletionService
        from masm.storage.repositories import MemoryRepository

        deleter = DeletionService(
            MemoryRepository(app.state.database), app.state.asset_store,
        )

        def cleanup_run(user_id: str, run_id: str) -> bool:
            return deleter.delete_run(run_id, user_id=user_id).complete

    cleanup_output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str]] = []
    with cleanup_output.open("x", encoding="utf-8") as manifest:
        cleanup_output.chmod(0o600)

        def register_cleanup(row: tuple[str, str]) -> None:
            rows.append(row)
            manifest.write("\t".join(row) + "\n")
            manifest.flush()
            os.fsync(manifest.fileno())

        try:
            with TestClient(app) as client:
                return evaluate_client(
                    client, cases, version=version, run_tag=run_tag,
                    api_key=api_key, register_cleanup=register_cleanup,
                    attempt_meters=attempt_meters,
                )
        finally:
            cleanup_failures = 0
            for user_id, run_id in rows:
                try:
                    complete = cleanup_run(user_id, run_id)
                except Exception:
                    complete = False
                if not complete:
                    cleanup_failures += 1
            if cleanup_failures:
                raise RuntimeError(f"评估运行清理未完成：{cleanup_failures} 个运行")


@contextmanager
def meter_runtime(
    runtime: Any,
    *,
    client_factory: Callable[[], httpx.Client] = httpx.Client,
    llm_attempt_limit: int | None = None,
    embedding_attempt_limit: int | None = None,
) -> Any:
    """只在本地评估期间给正式 Provider 注入计量 HTTP Client。"""
    llm = runtime.llm
    text_embeddings = getattr(runtime.embeddings, "_text_embeddings", None)
    if llm is None or text_embeddings is None:
        raise ValueError("计量仅支持正式 LLM 与文本 Embedding 运行时")
    previous_llm = llm._client
    previous_embedding = text_embeddings._client
    if previous_llm is not None or previous_embedding is not None:
        raise ValueError("Provider 已注入 HTTP Client，拒绝覆盖")
    with ExitStack() as stack:
        llm_meter = MeteredClient(
            stack.enter_context(client_factory()), provider="llm", model=llm.model,
            attempt_limit=llm_attempt_limit,
        )
        embedding_meter = MeteredClient(
            stack.enter_context(client_factory()), provider="embedding",
            model=text_embeddings.model_name,
            attempt_limit=embedding_attempt_limit,
        )
        llm._client = llm_meter
        text_embeddings._client = embedding_meter
        try:
            for provider, meter in ((llm, llm_meter), (text_embeddings, embedding_meter)):
                original_post = getattr(provider, "_post", None)
                if original_post is None:
                    continue

                def classify_post(*args: Any, _post: Any = original_post,
                                  _meter: MeteredClient = meter, **kwargs: Any) -> Any:
                    try:
                        return _post(*args, **kwargs)
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                        kind: Literal[
                            "http_status", "timeout", "transport", "response_validation"
                        ]
                        if isinstance(exc, httpx.HTTPStatusError):
                            kind = "http_status"
                            status_code = exc.response.status_code
                        elif isinstance(exc, httpx.TimeoutException):
                            kind = "timeout"
                            status_code = None
                        elif isinstance(exc, httpx.HTTPError):
                            kind = "transport"
                            status_code = None
                        else:
                            kind = "response_validation"
                            status_code = None
                        _meter.attempt_failures.append(AttemptFailureRecord(
                            _meter._provider, _CURRENT_STAGE.get(), kind, status_code,
                        ))
                        raise

                stack.enter_context(patch.object(provider, "_post", classify_post))
            yield (llm_meter, embedding_meter)
        finally:
            llm._client = previous_llm
            text_embeddings._client = previous_embedding


def summarize_agent_calls(records: Sequence[Any]) -> dict[str, int]:
    """按 Schema 输出类型统计逻辑 Agent 调用，不混同 HTTP 重试次数。"""
    return {
        "logical_llm_calls": len(records),
        "perception_calls": sum(row.output_type == "PerceptionResult" for row in records),
        "fused_text_calls": sum(row.output_type == "FusedTextResult" for row in records),
        "failed_logical_calls": sum(not row.succeeded for row in records),
    }


def validate_test_database_url(value: str) -> str:
    """硬性限制为本机 masm_test，绝不在错误中回显连接串。"""
    try:
        parsed = make_url(value)
    except Exception:
        raise ValueError("测试数据库连接串无效") from None
    if (
        parsed.host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.database != "masm_test"
        or parsed.query
    ):
        raise ValueError("评估仅允许本机 masm_test 数据库")
    return value


def resolve_runtime_profile(version: str, override: str | None) -> str:
    """明确区分源码版本与运行档位；旧报告的 v1.0 默认值保持可复现。"""
    if version not in {"v1.0", "v1.1"}:
        raise ValueError("未知候选版本")
    if override is not None and override not in {"official-baseline", "official-masm"}:
        raise ValueError("未知正式运行档位")
    if version == "v1.1" and override == "official-baseline":
        raise ValueError("v1.1 必须使用 official-masm")
    return override or ("official-baseline" if version == "v1.0" else "official-masm")


def parse_price_card(
    source: str | None,
    llm_input: str | None,
    llm_output: str | None,
    embedding_input: str | None,
) -> dict[str, Decimal] | None:
    """费率必须完整且带来源；未提供时始终将费用标为 unavailable。"""
    values = (llm_input, llm_output, embedding_input)
    if not any(value is not None for value in values) and not source:
        return None
    if (
        not source or llm_input is None or llm_output is None
        or embedding_input is None
    ):
        raise ValueError("估算费用必须同时提供三项费率与价格出处")
    try:
        prices = {
            "llm_input_usd": Decimal(llm_input),
            "llm_output_usd": Decimal(llm_output),
            "embedding_input_cny": Decimal(embedding_input),
        }
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("费率必须是有效数字") from exc
    if any(not price.is_finite() or price < 0 for price in prices.values()):
        raise ValueError("费率必须是非负有限数字")
    return prices


def main(argv: Sequence[str] | None = None) -> int:
    """逐版本在本机正式 Provider 档位运行；永不连接生产数据库。"""
    parser = argparse.ArgumentParser(description="MASM 本地留出集与用量对照")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version", choices=("v1.0", "v1.1"), required=True)
    parser.add_argument(
        "--runtime-profile", choices=("official-baseline", "official-masm"), default=None,
    )
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup-output", type=Path, required=True)
    parser.add_argument("--run-tag", default=None)
    parser.add_argument(
        "--case-set",
        choices=("primary", "alternate-v1", "media-v1", "external", "external-media"),
        default="primary",
    )
    parser.add_argument("--external-cases", type=Path, default=None)
    parser.add_argument("--external-cases-sha256", default=None)
    parser.add_argument("--price-source", default=None)
    parser.add_argument("--llm-input-rate-usd", default=None)
    parser.add_argument("--llm-output-rate-usd", default=None)
    parser.add_argument("--embedding-input-rate-cny", default=None)
    parser.add_argument("--max-llm-attempts", type=int, default=None)
    parser.add_argument("--max-embedding-attempts", type=int, default=None)
    args = parser.parse_args(argv)

    if any(
        limit is not None and limit < 1
        for limit in (args.max_llm_attempts, args.max_embedding_attempts)
    ):
        raise ValueError("Provider HTTP 尝试上限必须为正整数")
    profile_name = resolve_runtime_profile(args.version, args.runtime_profile)

    database_url = validate_test_database_url(os.getenv("MASM_TEST_DATABASE_URL", ""))
    prices = parse_price_card(
        args.price_source, args.llm_input_rate_usd, args.llm_output_rate_usd,
        args.embedding_input_rate_cny,
    )
    source_root = args.source_root.resolve()
    if not (source_root / "src" / "masm").is_dir():
        raise ValueError("源码根目录无效")
    asset_dir = args.asset_dir.resolve()
    if not asset_dir.is_relative_to(source_root) or ".superpowers" not in asset_dir.parts:
        raise ValueError("评估资产目录必须位于源码树内的 .superpowers 目录")
    if args.output.resolve() == args.cleanup_output.resolve():
        raise ValueError("报告与清理清单不能使用同一路径")
    run_tag = args.run_tag or uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", run_tag):
        raise ValueError("运行标记只能包含字母、数字和连字符，最多 32 字符")
    if args.case_set in {"external", "external-media"}:
        if args.external_cases is None or args.external_cases_sha256 is None:
            raise ValueError("外部样本必须同时指定文件和 SHA-256")
        loader = (
            load_external_cases
            if args.case_set == "external"
            else load_external_media_cases
        )
        cases, external_source = loader(
            args.external_cases, expected_sha256=args.external_cases_sha256,
        )
    else:
        if args.external_cases is not None or args.external_cases_sha256 is not None:
            raise ValueError("内置模板不能同时指定外部样本")
        cases = build_holdout_cases(run_tag, case_set=args.case_set)
        external_source = None

    sys.path.insert(0, str(source_root / "src"))
    from masm.api.app import create_app
    from masm.config import RuntimeProfile, Settings

    profile = RuntimeProfile(profile_name)
    local_key = uuid4().hex
    settings = replace(
        Settings.from_env(), database_url=database_url, asset_dir=asset_dir,
        api_keys=(local_key,), runtime_profile=profile,
    )
    app = create_app(settings)
    with meter_runtime(
        app.state.runtime,
        llm_attempt_limit=args.max_llm_attempts,
        embedding_attempt_limit=args.max_embedding_attempts,
    ) as meters:
        with meter_app_stages(app) as stage_records:
            report = run_app_holdout(
                app, cases, version=args.version, run_tag=run_tag, api_key=local_key,
                cleanup_output=args.cleanup_output,
                attempt_meters=meters,
            )
    usage = summarize_usage(
        [record for meter in meters for record in meter.records],
        prices_per_million=prices,
    )
    logical_calls = summarize_agent_calls(app.state.runtime.llm.records)
    report.update({
        "embedding_inputs": usage["embedding_inputs"],
        "llm_calls": usage["llm_calls"],
        "perception_calls": logical_calls["perception_calls"],
        "fused_text_calls": logical_calls["fused_text_calls"],
        "cost": usage["estimated_cost"],
    })
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_root, text=True,
        capture_output=True, check=False,
    )
    output = {
        "version": args.version,
        "source_commit": revision.stdout.strip() if revision.returncode == 0 else "unavailable",
        "runtime": app.state.runtime_metadata,
        "case_count": len(cases),
        "case_set": args.case_set,
        "run_tag": run_tag,
        "report": report,
        "provider_usage": usage,
        "agent_calls": logical_calls,
        "diagnostics": build_diagnostic_summary(stage_records, meters),
        "price_source": args.price_source or "unavailable",
        "billing_status": "not_reconciled",
    }
    if external_source is not None:
        output["external_source"] = external_source
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(output, destination, ensure_ascii=False, sort_keys=True, indent=2)
        destination.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
