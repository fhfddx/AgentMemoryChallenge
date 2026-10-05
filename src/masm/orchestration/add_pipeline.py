"""Add 写入流水线的显式状态机编排器。

状态机只允许声明过的迁移，每个状态至多进入一次，因此智能体不会被递归或无边界地调用。
任何智能体或校验失败都会降级为基线记忆（原始证据 + 基础摘要 + 可检索向量），
已有记忆不受影响。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from masm.agents import MAX_HISTORY, resolve_max_history
from masm.agents.curator import MemoryCuratorAgent
from masm.agents.fused_text import FusedTextAgent, has_one_supported_date
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import AgentOutputError, TemporalRelationAgent
from masm.orchestration.action_validator import validate_actions
from masm.providers.llm import StructuredOutputError
from masm.retrieval.baseline import BaselineRetriever, ParsedQuery
from masm.schemas.agents import CuratorDecision, PerceptionResult, TemporalRelationResult
from masm.schemas.api import AddRequest
from masm.schemas.content import ContentPart, TextPart
from masm.storage.types import MemoryBundle, MemoryDraft, ValidatedActions

# 召回并传给智能体的同用户历史候选上限；不得超过集中定义的硬上限。
DEFAULT_MAX_HISTORY = MAX_HISTORY
class PipelineState(StrEnum):
    """Add 流水线的显式状态。"""

    CREATED = "created"
    PARSED = "parsed"
    PERCEIVED = "perceived"
    RECALLED = "recalled"
    TEMPORALISED = "temporalised"
    CURATED = "curated"
    VALIDATED = "validated"
    DEGRADED = "degraded"
    COMPLETED = "completed"


_ALLOWED_TRANSITIONS: Mapping[PipelineState, frozenset[PipelineState]] = {
    PipelineState.CREATED: frozenset({PipelineState.PARSED}),
    PipelineState.PARSED: frozenset({PipelineState.PERCEIVED, PipelineState.DEGRADED}),
    PipelineState.PERCEIVED: frozenset({PipelineState.RECALLED, PipelineState.DEGRADED}),
    PipelineState.RECALLED: frozenset({PipelineState.TEMPORALISED, PipelineState.DEGRADED}),
    PipelineState.TEMPORALISED: frozenset({PipelineState.CURATED, PipelineState.DEGRADED}),
    PipelineState.CURATED: frozenset({PipelineState.VALIDATED, PipelineState.DEGRADED}),
    PipelineState.VALIDATED: frozenset({PipelineState.COMPLETED}),
    PipelineState.DEGRADED: frozenset(),
    PipelineState.COMPLETED: frozenset(),
}


class PipelineStateError(RuntimeError):
    """状态机发生非法迁移或状态被重复进入。"""


class _StateMachine:
    """显式状态机：只允许声明的迁移，且每个状态只能进入一次。"""

    def __init__(self) -> None:
        self.state = PipelineState.CREATED
        self.trace: list[PipelineState] = [PipelineState.CREATED]
        self._visited = {PipelineState.CREATED}

    def advance(self, target: PipelineState) -> None:
        """迁移到目标状态；非法迁移或重复进入直接失败。"""
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise PipelineStateError(f"非法状态迁移: {self.state} -> {target}")
        if target in self._visited:
            raise PipelineStateError(f"状态不得重复进入: {target}")
        self.state = target
        self._visited.add(target)
        self.trace.append(target)


@dataclass(frozen=True)
class PipelineResult:
    """一次编排的结果：记忆束、访问过的状态轨迹与降级标记。"""

    bundle: MemoryBundle
    states: tuple[PipelineState, ...]
    degraded: bool
    failure: str | None = None


class AddPipeline:
    """显式状态机编排器：感知 -> 召回 -> 时序 -> 管理 -> 校验 -> 完成/降级。"""

    def __init__(
        self,
        *,
        perception: PerceptionAgent,
        temporal: TemporalRelationAgent,
        curator: MemoryCuratorAgent,
        retriever: BaselineRetriever,
        fused_text: FusedTextAgent | None = None,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        self._perception = perception
        self._temporal = temporal
        self._curator = curator
        self._retriever = retriever
        self._fused_text = fused_text
        self._max_history = resolve_max_history(max_history)

    @property
    def max_history(self) -> int:
        """历史候选固定上限。"""
        return self._max_history

    def process(self, request: AddRequest) -> MemoryBundle:
        """执行流水线并返回要持久化的记忆束（失败时返回降级基线记忆束）。"""
        return self.run(request).bundle

    def run(self, request: AddRequest) -> PipelineResult:
        """执行状态机并返回带轨迹的结果。"""
        machine = _StateMachine()
        try:
            content = _content_parts(request)
            machine.advance(PipelineState.PARSED)

            fused_result = None
            if self._can_fuse(request, content):
                assert self._fused_text is not None
                try:
                    fused_result = self._fused_text.extract(
                        tuple(part for part in content if isinstance(part, TextPart))
                    )
                except (AgentOutputError, StructuredOutputError, ValidationError):
                    # 融合输出不合规时保留原两阶段模型路径；传输失败仍按原规则降级。
                    pass
            perception = (
                fused_result.perception if fused_result is not None
                else self._perception.extract(content)
            )
            machine.advance(PipelineState.PERCEIVED)

            history = self._recall(request)
            machine.advance(PipelineState.RECALLED)

            # 预检之后可能有并发写入；出现候选时重做有历史时序分析。
            temporal = (
                TemporalRelationResult(
                    event_time=fused_result.event_time,
                    time_precision=fused_result.time_precision,
                ) if fused_result is not None and not history
                else self._temporal.analyze(perception, history)
            )
            machine.advance(PipelineState.TEMPORALISED)

            # 没有候选时治理动作没有可引用的目标；新记忆仍由 _bundle_from 创建。
            decision = (
                self._curator.propose(_draft_from(request, content, perception, temporal), history)
                if history else CuratorDecision()
            )
            machine.advance(PipelineState.CURATED)

            actions = validate_actions(request.user_id, decision, history)
            machine.advance(PipelineState.VALIDATED)

            bundle = _bundle_from(request, content, perception, temporal, actions)
            machine.advance(PipelineState.COMPLETED)
        except PipelineStateError:
            raise
        except Exception as exc:
            machine.advance(PipelineState.DEGRADED)
            return PipelineResult(
                bundle=_degraded_bundle(request, _content_parts(request)),
                states=tuple(machine.trace),
                degraded=True,
                failure=type(exc).__name__,
            )
        return PipelineResult(
            bundle=bundle, states=tuple(machine.trace), degraded=False, failure=None
        )

    def _can_fuse(self, request: AddRequest, content: tuple[ContentPart, ...]) -> bool:
        """仅无历史纯文本、恰有一个可核验日期且无紧凑日期歧义时启用。"""
        return (
            self._fused_text is not None
            and bool(content)
            and all(isinstance(part, TextPart) for part in content)
            and has_one_supported_date(
                tuple(part for part in content if isinstance(part, TextPart))
            )
            and (
                self._max_history == 0
                or not self._retriever.repository.has_context_memory(request.user_id)
            )
        )

    def _recall(self, request: AddRequest) -> list:
        """在同一用户范围内召回受限的历史候选。"""
        if self._max_history == 0:
            return []
        text = " ".join(_text_parts(request)).strip()
        if not text:
            return []
        if not self._retriever.repository.has_context_memory(request.user_id):
            return []
        return self._retriever.retrieve(
            request.user_id,
            ParsedQuery(text_queries=(text,), intent="fact"),
            self._max_history,
            granularity="context",
        )


def _content_parts(request: AddRequest) -> tuple[ContentPart, ...]:
    """按原始顺序展开全部内容分片。"""
    parts: list[ContentPart] = []
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            if content.strip():
                parts.append(TextPart(text=content))
        else:
            parts.extend(content)
    return tuple(parts)


def _text_parts(request: AddRequest) -> list[str]:
    """提取全部文本分片（纯文本消息或文本内容分片）。"""
    texts: list[str] = []
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(part.text for part in content if isinstance(part, TextPart))
    return texts


def _modality(content: Sequence[ContentPart]) -> str:
    has_text = any(isinstance(part, TextPart) for part in content)
    has_image = any(not isinstance(part, TextPart) for part in content)
    if has_text and has_image:
        return "mixed"
    return "image" if has_image else "text"


def _baseline_summary(content: Sequence[ContentPart]) -> str:
    """降级使用的基础摘要：只依赖原始内容，不依赖任何模型。"""
    text = " ".join(part.text for part in content if isinstance(part, TextPart)).strip()
    if text:
        return text
    if any(not isinstance(part, TextPart) for part in content):
        return "image"
    return ""


def _draft_from(
    request: AddRequest,
    content: Sequence[ContentPart],
    perception: PerceptionResult,
    temporal: TemporalRelationResult,
) -> MemoryDraft:
    """构造交给记忆管理智能体的新记忆草稿。"""
    descriptions = " ".join(description.description for description in perception.descriptions)
    summary = _baseline_summary(content) or descriptions
    return MemoryDraft(
        summary=summary,
        original_text=" ".join(_text_parts(request)).strip(),
        keywords=perception.keywords,
        modality=perception.modality or _modality(content),
        event_time=temporal.event_time,
        time_precision=temporal.time_precision.value,
        confidence=perception.entities[0].confidence if perception.entities else None,
    )


def _bundle_from(
    request: AddRequest,
    content: Sequence[ContentPart],
    perception: PerceptionResult,
    temporal: TemporalRelationResult,
    actions: ValidatedActions,
) -> MemoryBundle:
    """成功路径的记忆束：结构化记忆 + 已校验动作。"""
    return MemoryBundle(
        session_id=request.session_id,
        request_id=request.request_id,
        memories=(_draft_from(request, content, perception, temporal),),
        actions=actions,
    )


def _degraded_bundle(request: AddRequest, content: Sequence[ContentPart]) -> MemoryBundle:
    """降级路径的记忆束：原始证据 + 基础摘要，不含任何智能体结论。"""
    return MemoryBundle(
        session_id=request.session_id,
        request_id=request.request_id,
        memories=(
            MemoryDraft(
                summary=_baseline_summary(content),
                original_text=" ".join(_text_parts(request)).strip(),
                modality=_modality(content),
            ),
        ),
        actions=None,
        degraded=True,
    )
