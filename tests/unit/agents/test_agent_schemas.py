"""智能体 Schema 与忠实性约束的单元测试。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from masm.schemas.agents import (
    Entity,
    EntityKind,
    ImageDescription,
    ObservedEvent,
    PerceptionResult,
    RelationKind,
    RelationSuggestion,
    TemporalExpression,
    TemporalRelationResult,
    TimePrecision,
)


def _entity(**overrides: object) -> dict:
    payload = {
        "name": "red bicycle",
        "kind": EntityKind.OBJECT,
        "confidence": 0.9,
        "evidence": "a red bicycle",
    }
    payload.update(overrides)
    return payload


def _relation(**overrides: object) -> dict:
    payload = {
        "kind": RelationKind.UPDATES,
        "target_memory_id": uuid4(),
        "confidence": 0.6,
        "evidence": "the bike is now blue",
    }
    payload.update(overrides)
    return payload


def test_valid_entity_is_accepted() -> None:
    entity = Entity(**_entity())
    assert entity.kind is EntityKind.OBJECT
    assert entity.confidence == 0.9


def test_unknown_entity_kind_is_rejected() -> None:
    """无效实体：未知类别必须被拒绝。"""
    with pytest.raises(ValidationError):
        Entity(**_entity(kind="spaceship"))


def test_entity_without_meaningful_name_is_rejected() -> None:
    """无效实体：空名或纯标点名必须被拒绝。"""
    with pytest.raises(ValidationError):
        Entity(**_entity(name=" ... "))


def test_entity_without_evidence_is_rejected() -> None:
    """无来源实体必须被拒绝。"""
    with pytest.raises(ValidationError):
        Entity(**_entity(evidence=""))


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0])
def test_out_of_range_confidence_is_rejected(confidence: float) -> None:
    """越界置信度必须被拒绝。"""
    with pytest.raises(ValidationError):
        Entity(**_entity(confidence=confidence))


def test_relation_without_evidence_is_rejected() -> None:
    """无来源关系必须被拒绝。"""
    with pytest.raises(ValidationError):
        RelationSuggestion(**_relation(evidence=""))


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_relation_confidence_out_of_range_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        RelationSuggestion(**_relation(confidence=confidence))


def test_extra_fields_are_rejected() -> None:
    """智能体不得返回 Schema 之外的字段（例如最终答案）。"""
    with pytest.raises(ValidationError):
        Entity(**_entity(answer="42"))


def test_image_description_requires_text_and_keeps_ocr() -> None:
    with pytest.raises(ValidationError):
        ImageDescription(description="", confidence=0.5)
    description = ImageDescription(description="a dog", ocr_text="WOOF", confidence=0.5)
    assert description.ocr_text == "WOOF"


def test_observed_event_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ObservedEvent(description="jumped", confidence=0.5, evidence="")


def test_perception_result_defaults_are_empty() -> None:
    result = PerceptionResult()
    assert result.descriptions == ()
    assert result.entities == ()
    assert result.events == ()
    assert result.keywords == ()
    assert result.modality == "text"


def test_temporal_result_serializes_for_storage() -> None:
    memory_id = uuid4()
    result = TemporalRelationResult(
        time_precision=TimePrecision.DAY,
        temporal_expressions=(
            TemporalExpression(text="yesterday", precision=TimePrecision.DAY, confidence=0.7),
        ),
        relations=(RelationSuggestion(**_relation(target_memory_id=memory_id)),),
        event_order=("walked the dog",),
    )

    assert result.relations[0].target_memory_id == memory_id
    dumped = result.model_dump(mode="json")
    assert dumped["relations"][0]["target_memory_id"] == str(memory_id)
    assert dumped["time_precision"] == "day"


def test_agent_schemas_are_immutable() -> None:
    result = PerceptionResult()
    with pytest.raises(ValidationError):
        result.language = "en"
