"""认证契约测试。"""

from uuid import uuid4

from fastapi.testclient import TestClient


def _payload(user_id: str) -> dict:
    return {
        "request_id": f"req-{uuid4().hex}",
        "user_id": user_id,
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_add_requires_auth(client: TestClient) -> None:
    """缺少认证时 /add 返回 401。"""
    response = client.post("/add", json=_payload(f"u-{uuid4().hex}"))
    assert response.status_code == 401


def test_bearer_auth_accepted(client: TestClient) -> None:
    """Authorization: Bearer <key> 被接受。"""
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}"),
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200


def test_token_auth_accepted(client: TestClient) -> None:
    """Authorization: Token <key> 被接受。"""
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}"),
        headers={"Authorization": "Token test-key"},
    )
    assert response.status_code == 200


def test_x_api_key_accepted(client: TestClient) -> None:
    """X-Api-Key 被接受。"""
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}"),
        headers={"X-Api-Key": "test-key"},
    )
    assert response.status_code == 200


def test_invalid_auth_rejected(client: TestClient) -> None:
    """错误的密钥返回 401。"""
    response = client.post(
        "/add",
        json=_payload(f"u-{uuid4().hex}"),
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_health_requires_no_auth(client: TestClient) -> None:
    """Health 始终无需认证。"""
    response = client.get("/health")
    assert response.status_code == 200
