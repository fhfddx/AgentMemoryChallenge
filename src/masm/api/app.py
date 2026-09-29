"""FastAPI 应用工厂。"""

from collections.abc import Mapping

from fastapi import FastAPI

from masm.api.health import probe_dependencies
from masm.api.limits import RequestLimiter
from masm.api.routes import router
from masm.config import Settings
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.llm import StructuredLLM
from masm.retrieval.response_packer import ResponsePacker
from masm.runtime import build_runtime
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


def create_app(
    settings: Settings,
    *,
    database: Database | None = None,
    asset_store: AssetStore | None = None,
    limiter: RequestLimiter | None = None,
    embeddings: EmbeddingProvider | None = None,
    llm: StructuredLLM | None = None,
    channel_weights: Mapping[str, float] | None = None,
    packer: ResponsePacker | None = None,
) -> FastAPI:
    """根据配置构建 FastAPI 应用。"""
    application = FastAPI(title="MASM", version="0.1.0")
    application.state.settings = settings
    application.state.database = database or Database.create(settings.database_url)
    application.state.asset_store = asset_store or AssetStore(settings.asset_dir)
    application.state.limiter = limiter or RequestLimiter()
    repository = MemoryRepository(application.state.database)
    runtime = build_runtime(
        settings,
        repository,
        embeddings=embeddings,
        llm=llm,
        channel_weights=channel_weights,
    )
    application.state.runtime = runtime
    application.state.runtime_metadata = runtime.audit_metadata()
    application.state.embeddings = runtime.embeddings
    # 仅供容器编排/运维代码直接调用，不注册新的公共 HTTP 路由。
    application.state.dependency_probe = lambda: probe_dependencies(
        application.state.database, application.state.asset_store
    )

    application.state.add_service = AddService(
        repository,
        application.state.asset_store,
        settings,
        embeddings=application.state.embeddings,
        pipeline=runtime.add_pipeline,
    )
    # 官方 /search 路径始终受响应字节上限保护，不依赖调用方手工注入。
    application.state.search_service = SearchService(
        runtime.retriever,
        max_image_bytes=settings.max_image_bytes,
        analyzer=runtime.query_analyzer,
        expander=runtime.relation_expander,
        reranker=runtime.reranker,
        packer=packer
        if packer is not None
        else ResponsePacker(max_bytes=settings.max_search_response_bytes),
    )

    application.include_router(router)
    return application


def create_app_from_env() -> FastAPI:
    """供 Uvicorn ``--factory`` 使用的环境变量应用工厂。"""
    return create_app(Settings.from_env())
