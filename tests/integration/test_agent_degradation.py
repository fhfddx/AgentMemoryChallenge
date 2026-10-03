"""智能体故障降级集成测试：重试一次后必须保存可检索的基线记忆。"""

import json
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

from masm.agents.curator import MemoryCuratorAgent
from masm.agents.perception import PerceptionAgent
from masm.agents.temporal import TemporalRelationAgent
from masm.orchestration.add_pipeline import AddPipeline, PipelineState
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import MAX_ATTEMPTS, OpenAICompatibleLLM
from masm.retrieval.baseline import DEFAULT_CHANNEL_WEIGHTS, BaselineRetriever
from masm.schemas.agents import (
    ActionKind,
    CuratorAction,
    CuratorDecision,
    PerceptionResult,
    TemporalRelationResult,
)
from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Memory
from masm.storage.repositories import MemoryRepository

_VALID_BODY = {"choices": [{"message": {"content": '{"keywords": ["cat"], "language": "en"}'}}]}
_NON_JSON_BODY = {"choices": [{"message": {"content": "definitely not json"}}]}
_SCHEMA_INVALID_BODY = {"choices": [{"message": {"content": '{"modality": "nope"}'}}]}


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _create_action() -> CuratorAction:
    return CuratorAction(kind=ActionKind.CREATE, confidence=0.8, evidence="observed evidence")


def _pipeline_with_perception_llm(database, embeddings, perception_llm, *, decision=None):
    curator_llm = FakeStructuredLLM(
        [decision if decision is not None else CuratorDecision(actions=(_create_action(),))]
    )
    return AddPipeline(
        perception=PerceptionAgent(perception_llm),
        temporal=TemporalRelationAgent(FakeStructuredLLM([TemporalRelationResult()])),
        curator=MemoryCuratorAgent(curator_llm),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
    )


def _http_llm(handler) -> OpenAICompatibleLLM:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM(
        model="gpt-4o-mini",
        base_url="https://models.invalid/v1",
        api_key="test-key",
        client=client,
    )


def _search(database, settings, embeddings, user_id: str):
    search = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    )
    return search.search(SearchRequest(query="durable", user_id=user_id, top_k=10))


def _count(database: Database, model: type, user_id: str) -> int:
    with database.session() as session:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ).scalar_one()
        )


def _run_degraded_add(
    database, asset_store, settings, embeddings, perception_llm, *, decision=None
):
    pipeline = _pipeline_with_perception_llm(
        database, embeddings, perception_llm, decision=decision
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    user_id = _uid("u")
    response = service.add(_request(_uid("r"), user_id, "durable degraded memory"))
    return user_id, response


def test_provider_transport_failure_degrades_to_searchable_baseline(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """Provider 超时：只重试一次，然后降级保存可检索的基线记忆。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("timeout")

    user_id, response = _run_degraded_add(
        database, asset_store, settings, embeddings, _http_llm(handler)
    )

    assert response.success is True
    assert len(calls) == MAX_ATTEMPTS
    assert MemoryRepository(database).get_ledger_status(user_id, response.request_id) == "COMMITTED"
    assert _count(database, Memory, user_id) == 2
    assert _search(database, settings, embeddings, user_id).data


def test_non_json_output_degrades_to_searchable_baseline(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_NON_JSON_BODY)

    user_id, response = _run_degraded_add(
        database, asset_store, settings, embeddings, _http_llm(handler)
    )

    assert response.success is True
    assert len(calls) == MAX_ATTEMPTS
    assert _count(database, Memory, user_id) == 2
    assert _search(database, settings, embeddings, user_id).data


def test_schema_violation_degrades_to_searchable_baseline(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SCHEMA_INVALID_BODY)

    user_id, response = _run_degraded_add(
        database, asset_store, settings, embeddings, _http_llm(handler)
    )

    assert response.success is True
    assert len(calls) == MAX_ATTEMPTS
    assert _count(database, Memory, user_id) == 2
    assert _search(database, settings, embeddings, user_id).data


def test_curator_schema_error_degrades(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """Curator 返回无法校验的对象时同样降级，且不产生额外写入。"""
    pipeline = AddPipeline(
        perception=PerceptionAgent(FakeStructuredLLM([PerceptionResult(keywords=("cat",))])),
        temporal=TemporalRelationAgent(FakeStructuredLLM([TemporalRelationResult()])),
        curator=MemoryCuratorAgent(FakeStructuredLLM([RuntimeError("curator unavailable")])),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
    )
    user_id = _uid("u")

    result = pipeline.run(_request(_uid("r"), user_id, "durable memory"))

    assert result.degraded is True
    assert result.states[-1] is PipelineState.DEGRADED
    assert result.bundle.memories


def test_invalid_action_degrades_and_writes_nothing_unsafe(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """跨用户关系被校验器拒绝时，整条流水线降级为基线保存。"""
    foreign_candidate = CuratorDecision(
        actions=(
            CuratorAction(
                kind=ActionKind.LINK,
                target_memory_id=uuid4(),
                confidence=0.9,
                evidence="foreign",
            ),
        )
    )
    user_id, response = _run_degraded_add(
        database,
        asset_store,
        settings,
        embeddings,
        FakeStructuredLLM([PerceptionResult(keywords=("cat",))]),
        decision=foreign_candidate,
    )

    assert response.success is True
    assert _count(database, Memory, user_id) == 2
    assert _search(database, settings, embeddings, user_id).data


def test_degraded_bundle_has_no_actions(
    database: Database, embeddings
) -> None:
    pipeline = AddPipeline(
        perception=PerceptionAgent(FakeStructuredLLM([RuntimeError("unavailable")])),
        temporal=TemporalRelationAgent(FakeStructuredLLM([TemporalRelationResult()])),
        curator=MemoryCuratorAgent(FakeStructuredLLM([CuratorDecision()])),
        retriever=BaselineRetriever(
            MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS
        ),
    )

    result = pipeline.run(_request(_uid("r"), _uid("u"), "baseline fallback"))

    assert result.degraded is True
    assert result.bundle.actions is None
    assert result.bundle.degraded is True


def test_baseline_mode_without_pipeline_still_works(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """任务 4 的基线模式（不注入编排器）必须保持可用。"""
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings
    )
    user_id = _uid("u")

    response = service.add(_request(_uid("r"), user_id, "baseline only memory"))

    assert response.success is True
    assert _count(database, Memory, user_id) == 2
    results = SearchService(
        BaselineRetriever(MemoryRepository(database), embeddings, DEFAULT_CHANNEL_WEIGHTS),
        max_image_bytes=settings.max_image_bytes,
    ).search(SearchRequest(query="baseline", user_id=user_id, top_k=10))
    assert results.data


def test_valid_response_is_not_degraded(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """成功路径不得被误判为降级。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_VALID_BODY)

    pipeline = _pipeline_with_perception_llm(
        database, embeddings, _http_llm(handler)
    )
    service = AddService(
        MemoryRepository(database), asset_store, settings, embeddings=embeddings, pipeline=pipeline
    )
    user_id = _uid("u")

    response = service.add(_request(_uid("r"), user_id, "not degraded memory"))

    assert response.success is True
    assert len(calls) == 1
    with database.session() as session:
        memory = session.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.granularity == "context")
        ).scalar_one()
    assert memory.keywords == ["cat"]
    assert json.dumps(memory.keywords)


def test_memory_row_is_uuid_shaped(
    database: Database, asset_store: AssetStore, settings, embeddings
) -> None:
    """降级路径写入的记忆必须带有正常主键。"""
    _user_id, response = _run_degraded_add(
        database,
        asset_store,
        settings,
        embeddings,
        FakeStructuredLLM([RuntimeError("unavailable")]),
    )
    commit = MemoryRepository(database).get_by_request(response.user_id, response.request_id)
    assert commit is not None
    assert isinstance(commit.memory_ids[0], UUID)
