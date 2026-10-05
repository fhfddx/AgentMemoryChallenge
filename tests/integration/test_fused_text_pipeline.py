"""融合写入路径的本机数据库与编排边界测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.fused_text import FusedTextAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
from masm.schemas.agents import (
    CuratorDecision,
    FusedTextResult,
    PerceptionResult,
    TemporalRelationResult,
    TimePrecision,
)
from masm.schemas.api import AddRequest
from masm.schemas.content import ImageURLPart
from masm.services.add_service import AddService
from masm.storage.models import Memory, SessionRecord, SourceMessage
from masm.storage.repositories import MemoryRepository


def _request(user_id: str, content: str | list, *, session_id: str = "session-1") -> AddRequest:
    return AddRequest(
        request_id=uuid4().hex, user_id=user_id, session_id=session_id,
        messages=[{"role": "user", "content": content}],
    )


def _pipeline(
    database, embeddings, fused_response, *, max_history=8,
    temporal_response: TemporalRelationResult | None = None,
    enable_fusion: bool = True,
):
    fused_llm = FakeStructuredLLM([fused_response])
    perception_llm = FakeStructuredLLM([PerceptionResult(keywords=("legacy",))])
    temporal_llm = FakeStructuredLLM([temporal_response or TemporalRelationResult()])
    curator_llm = FakeStructuredLLM([CuratorDecision()])
    repository = MemoryRepository(database)
    pipeline = AddPipeline(
        perception=PerceptionAgent(perception_llm),
        temporal=TemporalRelationAgent(temporal_llm),
        curator=MemoryCuratorAgent(curator_llm),
        retriever=BaselineRetriever(repository, embeddings, DEFAULT_CHANNEL_WEIGHTS),
        fused_text=FusedTextAgent(fused_llm) if enable_fusion else None,
        max_history=max_history,
    )
    return pipeline, repository, fused_llm, perception_llm, temporal_llm


@pytest.mark.parametrize(
    ("content", "event_time"),
    [
        ("Meeting on 2026-10-03.", datetime(2026, 10, 3, tzinfo=UTC)),
        ("Meeting moved from October fourth to 2026-10-03.",
         datetime(2026, 10, 3, tzinfo=UTC)),
        ("Today is 2026-10-03. Meeting tomorrow.",
         datetime(2026, 10, 4, tzinfo=UTC)),
        ("Use the Saffron-fwlat6e20261004 folder on 2026-10-03.",
         datetime(2026, 10, 3, tzinfo=UTC)),
        ("Meeting on October 3, 2026.", datetime(2026, 10, 3, tzinfo=UTC)),
        ("Meet in the gallery.", None),
    ],
)
def test_release_path_persists_legacy_temporal_result_for_registered_cases(
    database, asset_store, settings, embeddings, content: str,
    event_time: datetime | None,
) -> None:
    """默认关闭融合时，旧时序输出不得被编排器静默丢弃。"""
    temporal_response = TemporalRelationResult(
        event_time=event_time,
        time_precision=TimePrecision.DAY if event_time else TimePrecision.UNKNOWN,
    )
    pipeline, repository, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
        temporal_response=temporal_response,
        enable_fusion=False,
    )
    request = _request(f"fused-{uuid4().hex}", content)

    response = AddService(
        repository, asset_store, settings, embeddings=embeddings, pipeline=pipeline,
    ).add(request)

    assert response.success is True
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1
    with database.session() as session:
        stored = session.execute(select(Memory).where(
            Memory.user_id == request.user_id, Memory.granularity == "context",
        )).scalar_one()
    assert stored.event_time == event_time
    assert stored.time_precision == ("day" if event_time else "unknown")


def test_release_path_uses_same_user_history_for_cross_session_reschedule(
    database, asset_store, settings, embeddings,
) -> None:
    user_id = f"fused-{uuid4().hex}"
    other_user_id = f"fused-{uuid4().hex}"
    repository = MemoryRepository(database)
    baseline = AddService(repository, asset_store, settings, embeddings=embeddings)
    baseline.add(_request(user_id, "Meeting on 2026-10-03.", session_id="session-old"))
    baseline.add(_request(
        other_user_id, "Other user meeting on 2026-10-03.", session_id="session-other",
    ))
    event_time = datetime(2026, 10, 4, tzinfo=UTC)
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
        temporal_response=TemporalRelationResult(
            event_time=event_time, time_precision=TimePrecision.DAY,
        ),
        enable_fusion=False,
    )
    request = _request(user_id, "Meeting moved to 2026-10-04.", session_id="session-new")

    response = AddService(
        repository, asset_store, settings, embeddings=embeddings, pipeline=pipeline,
    ).add(request)

    assert response.success is True
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1
    history = temporal.requests[0].payload["history"]
    assert len(history) == 1
    assert "Meeting on 2026-10-03." in history[0]["content"]
    assert "Other user" not in history[0]["content"]
    with database.session() as session:
        stored = session.execute(select(Memory).where(
            Memory.user_id == user_id, Memory.request_id == request.request_id,
            Memory.granularity == "context",
        )).scalar_one()
        source_sessions = session.execute(
            select(SessionRecord.session_id)
            .join(SourceMessage, SourceMessage.session_id == SessionRecord.id)
            .where(SourceMessage.user_id == user_id)
        ).scalars().all()
    assert stored.event_time == event_time
    assert stored.time_precision == "day"
    assert set(source_sessions) == {"session-old", "session-new"}


def test_empty_history_text_uses_one_fused_call_and_preserves_date(
    database, asset_store, settings, embeddings,
) -> None:
    event_time = datetime(2026, 10, 3, tzinfo=UTC)
    pipeline, repository, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(
            perception=PerceptionResult(keywords=("meeting",)),
            event_time=event_time, time_precision=TimePrecision.DAY,
            time_evidence="2026-10-03",
        ),
    )
    request = _request(f"fused-{uuid4().hex}", "Meeting on 2026-10-03")
    service = AddService(
        repository, asset_store, settings, embeddings=embeddings, pipeline=pipeline,
    )

    response = service.add(request)

    assert response.success is True
    assert len(fused.requests) == 1
    assert perception.requests == []
    assert temporal.requests == []
    with database.session() as session:
        stored = session.execute(select(Memory).where(
            Memory.user_id == request.user_id, Memory.granularity == "context",
        )).scalar_one()
    assert stored.event_time == event_time
    assert stored.time_precision == "day"


def test_natural_language_date_uses_legacy_temporal_result(
    database, asset_store, settings, embeddings,
) -> None:
    event_time = datetime(2026, 10, 3, tzinfo=UTC)
    pipeline, repository, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
        temporal_response=TemporalRelationResult(
            event_time=event_time, time_precision=TimePrecision.DAY,
        ),
    )
    request = _request(f"fused-{uuid4().hex}", "Meeting on October 3, 2026")
    service = AddService(
        repository, asset_store, settings, embeddings=embeddings, pipeline=pipeline,
    )

    response = service.add(request)

    assert response.success is True
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1
    with database.session() as session:
        stored = session.execute(select(Memory).where(
            Memory.user_id == request.user_id, Memory.granularity == "context",
        )).scalar_one()
    assert stored.event_time == event_time
    assert stored.time_precision == "day"


def test_no_explicit_date_uses_legacy_path(database, embeddings) -> None:
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
    )

    result = pipeline.run(_request(f"fused-{uuid4().hex}", "Meet in the gallery"))

    assert result.degraded is False
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_two_explicit_dates_use_legacy_path(database, embeddings) -> None:
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
    )

    result = pipeline.run(_request(
        f"fused-{uuid4().hex}", "Meeting moved from 2026-10-03 to 2026-10-04",
    ))

    assert result.degraded is False
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_existing_history_keeps_legacy_temporal_path(database, embeddings) -> None:
    pipeline, repository, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
    )
    user_id = f"fused-{uuid4().hex}"
    repository.add_bundle(user_id, _baseline_bundle("prior meeting"))

    result = pipeline.run(_request(user_id, "another meeting on 2026-10-03"))

    assert result.degraded is False
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_image_never_uses_text_fusion(database, embeddings) -> None:
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
    )
    image = ImageURLPart(image_url={"url": "data:image/png;base64,iVBORw0KGgo="})

    result = pipeline.run(_request(f"fused-{uuid4().hex}", [image]))

    assert result.degraded is False
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_invalid_fused_output_falls_back_to_legacy(database, embeddings) -> None:
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(
            perception=PerceptionResult(),
            event_time=datetime(2026, 10, 3, tzinfo=UTC),
            time_precision=TimePrecision.DAY,
            time_evidence="not in text",
        ),
    )

    result = pipeline.run(_request(f"fused-{uuid4().hex}", "meeting on 2026-10-03"))

    assert result.degraded is False
    assert len(fused.requests) == 1
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_compact_date_inside_identifier_skips_fused_call(database, embeddings) -> None:
    pipeline, _, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(perception=PerceptionResult()),
    )

    result = pipeline.run(_request(
        f"fused-{uuid4().hex}",
        "Use the Saffron-fwlat6e20261004 folder on 2026-10-03.",
    ))

    assert result.degraded is False
    assert fused.requests == []
    assert len(perception.requests) == 1
    assert len(temporal.requests) == 1


def test_history_appearing_after_preflight_uses_legacy_temporal(
    database, asset_store, settings, embeddings,
) -> None:
    pipeline, repository, fused, perception, temporal = _pipeline(
        database, embeddings,
        FusedTextResult(
            perception=PerceptionResult(),
            event_time=datetime(2026, 10, 3, tzinfo=UTC),
            time_precision=TimePrecision.DAY,
            time_evidence="2026-10-03",
        ),
    )
    user_id = f"fused-{uuid4().hex}"
    AddService(repository, asset_store, settings, embeddings=embeddings).add(
        _request(user_id, "prior meeting")
    )
    original = repository.has_context_memory
    checks = 0

    def delayed_history(candidate_user: str) -> bool:
        nonlocal checks
        checks += 1
        return False if checks == 1 else original(candidate_user)

    repository.has_context_memory = delayed_history

    result = pipeline.run(_request(user_id, "another meeting on 2026-10-03"))

    assert result.degraded is False
    assert len(fused.requests) == 1
    assert perception.requests == []
    assert len(temporal.requests) == 1
    assert temporal.requests[0].payload["history"]


def _baseline_bundle(text: str):
    from masm.storage.types import MemoryBundle, MemoryDraft

    return MemoryBundle(
        request_id=uuid4().hex, session_id="session-prior",
        memories=(MemoryDraft(summary=text, original_text=text, modality="text"),),
    )
