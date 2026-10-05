"""仅在无历史纯文本路径使用的一次感知、时序融合调用。"""

import re
from collections.abc import Sequence
from datetime import date

from masm.agents import load_prompt
from masm.agents.temporal import AgentOutputError
from masm.providers.llm import ModelRequest, StructuredLLM
from masm.schemas.agents import FusedTextResult, TimePrecision
from masm.schemas.content import TextPart

PROMPT_FILE = "fused_text_v2.txt"
PROMPT_VERSION = "fused-text-v2"
_STANDALONE_DATE = re.compile(
    r"(?<![A-Za-z0-9_/\\-])([0-9]{4})([-/])([0-9]{2})\2([0-9]{2})"
    r"(?![A-Za-z0-9_/\\-])"
)


def has_one_supported_date(content: Sequence[TextPart]) -> bool:
    """只让恰有一个可核验日历日期的文本进入融合试验。"""
    source = " ".join(part.text for part in content)
    matches = [
        match for match in _STANDALONE_DATE.finditer(source)
        if _parse_calendar_day(match) is not None
    ]
    if len(matches) != 1:
        return False
    match = matches[0]
    remaining = source[:match.start()] + source[match.end():]
    return not any(character.isdigit() for character in remaining)


class FusedTextAgent:
    """一次模型调用返回经过现有 Schema 与关系证据校验的两阶段结果。"""

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
        return self._prompt

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def extract(self, content: Sequence[TextPart]) -> FusedTextResult:
        """只接收按原序排列的文本；历史与图片由编排器排除。"""
        request = ModelRequest(
            prompt=self._prompt,
            content=tuple(content),
            model=self._model,
            prompt_version=self._prompt_version,
        )
        result = self._llm.complete_json(request, FusedTextResult)
        if result.perception.modality != "text":
            raise AgentOutputError("融合文本结果的模态不是 text")
        source = " ".join(part.text for part in content)
        if result.event_time is None:
            if (
                result.time_precision is not TimePrecision.UNKNOWN
                or result.time_evidence is not None
            ):
                raise AgentOutputError("融合结果中未知时间的字段不一致")
            if any(
                _parse_calendar_day(match) is not None
                for match in _STANDALONE_DATE.finditer(source)
            ):
                raise AgentOutputError("原文包含独立日期但融合结果缺少时间")
        elif (
            result.time_precision is TimePrecision.UNKNOWN
            or result.time_evidence is None
            or result.time_evidence not in source
        ):
            raise AgentOutputError("融合结果中的时间缺少原文证据或精度")
        elif result.time_precision is not TimePrecision.DAY or not _matches_source_day(
            source, result.time_evidence, result.event_time.date()
        ):
            raise AgentOutputError("融合结果中的归一化日期与独立原文日期不符")
        return result


def _matches_source_day(source: str, evidence: str, normalized: date) -> bool:
    """仅接受与模型日期一致、且不嵌在标识符里的原文日历日期。"""
    for match in _STANDALONE_DATE.finditer(source):
        if match.group() not in evidence:
            continue
        if _parse_calendar_day(match) == normalized:
            return True
    return False


def _parse_calendar_day(match: re.Match[str]) -> date | None:
    try:
        return date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
    except ValueError:
        return None
