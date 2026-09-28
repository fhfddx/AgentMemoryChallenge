"""感知智能体：把保持顺序的多模态内容转换为可观察的结构化结果。"""

from collections.abc import Sequence

from masm.agents import load_prompt
from masm.providers.llm import ModelRequest, StructuredLLM
from masm.schemas.agents import PerceptionResult
from masm.schemas.content import ContentPart

PROMPT_FILE = "perception_v1.txt"
PROMPT_VERSION = "v1"


class PerceptionAgent:
    """感知智能体。

    只依赖 Provider 与 Prompt：不接收 Repository 或数据库会话，也不写存储。
    """

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        model: str | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._llm = llm
        self._model = model or llm.model
        self._prompt_version = prompt_version
        self._prompt = load_prompt(PROMPT_FILE)

    @property
    def prompt(self) -> str:
        """当前 Prompt 文本（供审计与忠实性测试）。"""
        return self._prompt

    @property
    def prompt_version(self) -> str:
        """当前 Prompt 版本号。"""
        return self._prompt_version

    def extract(self, content: Sequence[ContentPart]) -> PerceptionResult:
        """从原始有序内容中抽取通过 Schema 校验的感知结果。

        内容分片以官方结构原样交给 Provider，由 Provider 适配层转换为厂商多模态内容块，
        因此视觉模型能够真正接收图片。
        """
        request = ModelRequest(
            prompt=self._prompt,
            content=tuple(content),
            model=self._model,
            prompt_version=self._prompt_version,
        )
        return self._llm.complete_json(request, PerceptionResult)
