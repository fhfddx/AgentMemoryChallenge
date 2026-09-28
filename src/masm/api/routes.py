"""公共 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from masm.api.auth import require_auth
from masm.schemas.api import AddRequest, AddResponse
from masm.services.add_service import AddRequestError
from masm.storage.assets import MediaValidationError

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """公开浅健康检查：只返回状态，不暴露任何依赖机密。"""
    return {"status": "ok"}


@router.post("/add")
async def add(
    request: AddRequest,
    req: Request,
    credential: str = Depends(require_auth),
) -> AddResponse:
    """写入记忆；经过认证与并发限制，异常映射为确定的状态码。"""
    service = req.app.state.add_service
    limiter = req.app.state.limiter
    async with limiter.acquire(credential):
        try:
            return service.add(request)
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except AddRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
