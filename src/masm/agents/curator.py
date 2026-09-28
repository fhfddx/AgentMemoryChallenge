"""记忆管理智能体：只提出结构化动作建议，不接触存储。"""

from collections.abc import Sequence

from masm.agents import load_prompt
from masm.providers.llm import ModelRequest, StructuredLLM
from masm.schemas.agents import CuratorDecision
from masm.storage.types import MemoryCandidate, MemoryDraft

PROMPT_FILE = "curator_v1.txt"
PROMPT_VERSION = "v1"

# 传入模型的同用户历史候选上限（配置项）。
DEFAULT_MAX_HISTORY = 8


class MemoryCuratorAgent:
    """记忆管理智能体。

    只依赖 Provider 与 Prompt：不接收 Repository 或数据库会话，也不写存储；
    提出的动作必须由确定性校验器（orchestration.action_validator）批准后才会执行。
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

    def propose(
        self, new_memory: MemoryDraft, history: Sequence[MemoryCandidate]
    ) -> CuratorDecision:
        """针对新记忆与受限历史提出 CREATE/LINK/MERGE/SUPERSEDE/CONFLICT 建议。"""
        bounded = list(history)[: self._max_history]
        request = ModelRequest(
            prompt=self._prompt,
            payload={
                "new_memory": {
                    "summary": new_memory.summary,
                    "original_text": new_memory.original_text,
                    "keywords": list(new_memory.keywords),
                    "modality": new_memory.modality,
                },
                "history": [
                    {"memory_id": str(candidate.memory_id), "content": candidate.content}
                    for candidate in bounded
                ],
            },
            model=self._model,
            prompt_version=self._prompt_version,
        )
        return self._llm.complete_json(request, CuratorDecision)
