"""API 认证（Bearer / Token / X-Api-Key）。"""

import hmac

from fastapi import HTTPException, Request

from masm.config import Settings


def extract_credential(request: Request) -> str | None:
    """从 X-Api-Key 或 Authorization (Bearer/Token) 提取凭证。"""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() in {"bearer", "token"} and value:
        return value
    return None


def require_auth(request: Request) -> str:
    """认证请求并返回凭证；缺少或无效认证时抛出 401。"""
    settings: Settings = request.app.state.settings
    credential = extract_credential(request)
    if not settings.api_keys:
        raise HTTPException(status_code=401, detail="缺少认证配置")
    if credential is None or not any(
        hmac.compare_digest(credential, key) for key in settings.api_keys
    ):
        raise HTTPException(status_code=401, detail="认证无效")
    return credential
