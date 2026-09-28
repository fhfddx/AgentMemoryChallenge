"""公共 API 路由。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """公开浅健康检查：只返回状态，不暴露任何依赖机密。"""
    return {"status": "ok"}
