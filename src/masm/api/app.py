"""FastAPI 应用工厂。"""

from pathlib import Path

from fastapi import FastAPI

from masm.api.limits import RequestLimiter
from masm.api.routes import router
from masm.config import Settings
from masm.services.add_service import AddService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


def create_app(
    settings: Settings,
    *,
    database: Database | None = None,
    asset_store: AssetStore | None = None,
    limiter: RequestLimiter | None = None,
) -> FastAPI:
    """根据配置构建 FastAPI 应用。"""
    application = FastAPI(title="MASM", version="0.1.0")
    application.state.settings = settings
    application.state.database = database or Database.create(settings.database_url)
    application.state.asset_store = asset_store or AssetStore(Path("artifacts/assets"))
    application.state.limiter = limiter or RequestLimiter()
    application.state.add_service = AddService(
        MemoryRepository(application.state.database),
        application.state.asset_store,
        settings,
    )
    application.include_router(router)
    return application
