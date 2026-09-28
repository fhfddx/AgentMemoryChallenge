"""FastAPI 应用工厂。"""

from fastapi import FastAPI

from masm.api.routes import router
from masm.config import Settings


def create_app(settings: Settings) -> FastAPI:
    """根据配置构建 FastAPI 应用。"""
    application = FastAPI(title="MASM", version="0.1.0")
    application.state.settings = settings
    application.include_router(router)
    return application
