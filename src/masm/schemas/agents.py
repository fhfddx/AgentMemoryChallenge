"""智能体结构化输出 Schema（感知智能体与时序关系智能体）。

忠实性约束在 Schema 层强制：未知类别、越界置信度、缺少原始证据引用的实体/事件/关系
都会被 Pydantic 拒绝，避免不可观察的推断进入存储。
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 派生字段置信度：越界必须被拒绝。
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

_MAX_ENTITIES = 64
_MAX_KEYWORDS = 64
_MAX_EVENTS = 64
_MAX_RELATIONS = 64


class EntityKind(StrEnum):
    """可直接观察到的实体类别。"""

    PERSON = "person"
    OBJECT = "object"
    PLACE = "place"
    SCENE = "scene"
    ORGANIZATION = "organization"
    OTHER = "other"


class TimePrecision(StrEnum):
    """事件时间精度。"""

    UNKNOWN = "unknown"
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"


class RelationKind(StrEnum):
    """时序关系智能体允许提出的关系类型。"""

    SUPPLEMENTS = "supplements"
    UPDATES = "updates"
    DUPLICATES = "duplicates"
    CONFLICTS = "conflicts"
    BEFORE = "before"
    AFTER = "after"
    SAME_EVENT = "same_event"


class _StrictModel(BaseModel):
    """智能体 Schema 共同约定：禁止多余字段，且不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _WithEvidence(_StrictModel):
    """带原始证据引用的派生字段：证据必须非空且不能仅含空白。"""

    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("evidence")
    @classmethod
    def _evidence_must_not_be_blank(cls, value: str) -> str:
        """拒绝空字符串与纯空白证据；原样返回，绝不改写原始证据引用。"""
        if not value.strip():
            raise ValueError("evidence 不能为空或仅含空白")
        return value


class Entity(_WithEvidence):
    """可直接观察到的实体；必须携带原始证据引用。"""

    name: str = Field(min_length=1, max_length=200)
    kind: EntityKind
    confidence: Confidence

    @field_validator("name")
    @classmethod
    def _name_must_contain_alphanumeric(cls, value: str) -> str:
        """实体名必须包含字母、数字或汉字，拒绝空名与纯标点。"""
        if not any(character.isalnum() for character in value):
            raise ValueError("实体名必须包含字母、数字或汉字")
        return value


class ObservedEvent(_WithEvidence):
    """可直接观察到的动作或事件；必须携带原始证据引用。"""

    description: str = Field(min_length=1, max_length=500)
    participants: tuple[str, ...] = ()
    confidence: Confidence


class ImageDescription(_StrictModel):
    """忠实的图片描述与 OCR 文本。"""

    description: str = Field(min_length=1, max_length=2000)
    ocr_text: str = Field(default="", max_length=4000)
    confidence: Confidence


class PerceptionResult(_StrictModel):
    """感知智能体输出：只包含可直接观察到的内容。"""

    descriptions: tuple[ImageDescription, ...] = Field(default=(), max_length=_MAX_EVENTS)
    entities: tuple[Entity, ...] = Field(default=(), max_length=_MAX_ENTITIES)
    events: tuple[ObservedEvent, ...] = Field(default=(), max_length=_MAX_EVENTS)
    keywords: tuple[str, ...] = Field(default=(), max_length=_MAX_KEYWORDS)
    language: str = Field(default="unknown", max_length=64)
    modality: Literal["text", "image", "mixed"] = "text"
    scene: str | None = Field(default=None, max_length=500)


class TemporalExpression(_StrictModel):
    """绝对或相对时间表达。"""

    text: str = Field(min_length=1, max_length=200)
    normalized: datetime | None = None
    precision: TimePrecision = TimePrecision.UNKNOWN
    confidence: Confidence


class RelationSuggestion(_WithEvidence):
    """针对某条同用户历史候选的关系建议；必须携带原始证据引用。"""

    kind: RelationKind
    target_memory_id: UUID
    confidence: Confidence


class TemporalRelationResult(_StrictModel):
    """时序关系智能体输出：只提出关系建议，不修改存储。"""

    event_time: datetime | None = None
    time_precision: TimePrecision = TimePrecision.UNKNOWN
    temporal_expressions: tuple[TemporalExpression, ...] = Field(
        default=(), max_length=_MAX_RELATIONS
    )
    relations: tuple[RelationSuggestion, ...] = Field(default=(), max_length=_MAX_RELATIONS)
    event_order: tuple[str, ...] = Field(default=(), max_length=_MAX_EVENTS)
