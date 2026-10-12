"""Dedicated Add-time relation linker with no storage access."""

from collections.abc import Sequence

from masm.agents import MAX_HISTORY, load_prompt, resolve_max_history
from masm.providers.llm import ModelRequest, StructuredLLM
from masm.schemas.agents import RelationLinkDecision
from masm.storage.types import MemoryCandidate, MemoryDraft

PROMPT_FILE = "relation_linker_v1.txt"
PROMPT_VERSION = "relation-linker-v1"


class RelationLinkerAgent:
    """Propose bounded, non-destructive links to supplied history candidates."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        model: str | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_history: int = MAX_HISTORY,
    ) -> None:
        self._llm = llm
        self._model = model or llm.model
        self._prompt_version = prompt_version
        self._max_history = resolve_max_history(max_history)
        self._prompt = load_prompt(PROMPT_FILE)

    @property
    def prompt(self) -> str:
        return self._prompt

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def max_history(self) -> int:
        return self._max_history

    def propose(
        self, new_memory: MemoryDraft, history: Sequence[MemoryCandidate]
    ) -> RelationLinkDecision:
        bounded = list(history)[: self._max_history]
        request = ModelRequest(
            prompt=self._prompt,
            payload={
                "current_memory": {"original_text": new_memory.original_text},
                "history": [
                    {"memory_id": str(candidate.memory_id), "content": candidate.content}
                    for candidate in bounded
                ],
            },
            model=self._model,
            prompt_version=self._prompt_version,
        )
        return self._llm.complete_json(request, RelationLinkDecision)
