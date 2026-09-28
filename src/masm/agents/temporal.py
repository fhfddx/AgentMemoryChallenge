"""时序关系智能体：基于感知结果与受限历史候选提出关系建议。"""

from collections.abc import Sequence

from masm.agents import load_prompt
from masm.providers.llm import ModelRequest, StructuredLLM
from masm.schemas.agents import PerceptionResult, TemporalRelationResult
from masm.storage.types import MemoryCandidate

PROMPT_FILE = "temporal_v1.txt"
PROMPT_VERSION = "v1"

# 传入模型的同用户历史候选上限（配置项）。
DEFAULT_MAX_HISTORY = 8


class AgentOutputError(ValueError):
    """智能体输出无法满足忠实性约束。"""


class TemporalRelationAgent:
    """时序关系智能体。

    只提出关系建议：不接收 Repository 或数据库会话，也不修改存储。
    """

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        model: str | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        if max_history < 0:
            raise ValueError("max_history 必须为非负整数")
        self._llm = llm
        self._model = model or llm.model
        self._prompt_version = prompt_version
        self._max_history = max_history
        self._prompt = load_prompt(PROMPT_FILE)

    @property
    def prompt(self) -> str:
        """当前 Prompt 文本（供审计与忠实性测试）。"""
        return self._prompt

    @property
    def prompt_version(self) -> str:
        """当前 Prompt 版本号。"""
        return self._prompt_version

    @property
    def max_history(self) -> int:
        """传入模型的历史候选上限。"""
        return self._max_history

    def analyze(
        self, perception: PerceptionResult, history: Sequence[MemoryCandidate]
    ) -> TemporalRelationResult:
        """提出时间、顺序与关系建议，并校验其可溯源性。"""
        bounded = list(history)[: self._max_history]
        request = ModelRequest(
            prompt=self._prompt,
            payload={
                "perception": perception.model_dump(mode="json"),
                "history": [
                    {"memory_id": str(candidate.memory_id), "content": candidate.content}
                    for candidate in bounded
                ],
            },
            model=self._model,
            prompt_version=self._prompt_version,
        )
        result = self._llm.complete_json(request, TemporalRelationResult)
        self._validate_grounding(result, bounded, perception)
        return result

    @staticmethod
    def _validate_grounding(
        result: TemporalRelationResult,
        history: Sequence[MemoryCandidate],
        perception: PerceptionResult,
    ) -> None:
        """关系只能指向本次提供的候选，事件顺序只能引用已感知的事件。"""
        allowed = {candidate.memory_id for candidate in history}
        for relation in result.relations:
            if relation.target_memory_id not in allowed:
                raise AgentOutputError("关系引用了未提供的候选记忆")
        observed = {event.description for event in perception.events}
        for description in result.event_order:
            if description not in observed:
                raise AgentOutputError("事件顺序引用了感知结果中不存在的事件")
