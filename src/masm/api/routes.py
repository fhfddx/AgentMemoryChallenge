"""公共 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from masm.api.auth import credential_fingerprint, require_auth
from masm.providers.errors import ProviderError
from masm.schemas.api import AddRequest, AddResponse, SearchRequest, SearchResponse
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
    async with limiter.acquire(credential_fingerprint(credential)):
        try:
            return service.add(request)
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except AddRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail="模型依赖不可用") from exc


@router.post("/search")
async def search(
    request: SearchRequest,
    req: Request,
    credential: str = Depends(require_auth),
) -> SearchResponse:
    """检索记忆证据；只返回证据，绝不生成最终答案。"""
    service = req.app.state.search_service
    limiter = req.app.state.limiter
    async with limiter.acquire(credential_fingerprint(credential)):
        try:
            return service.search(request)
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail="模型依赖不可用") from exc
