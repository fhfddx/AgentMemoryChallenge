"""基线混合检索：Add 后立即 Search 的集成测试（只使用 Fake Provider）。"""

import base64
import io
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _image_bytes(size: int = 8, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def _text_request(request_id: str, user_id: str, text: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[{"role": "user", "content": text}],
    )


def _image_request(request_id: str, user_id: str, data: bytes) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": _data_url(data)}}],
            }
        ],
    )


def test_add_then_search_by_text(add_service: AddService, search_service: SearchService) -> None:
    """Add 成功后立即可被文本检索到。"""
    user_id = _uid("u")
    add_service.add(_text_request(_uid("r"), user_id, "the cat sat on the mat"))

    response = search_service.search(SearchRequest(query="cat", user_id=user_id, top_k=10))

    assert response.data
    assert any("cat" in evidence.content for evidence in response.data)


def test_text_query_retrieves_image_basic_description(
    add_service: AddService, search_service: SearchService
) -> None:
    """文本查询能检索到图片的基础描述。"""
    user_id = _uid("u")
    add_service.add(_image_request(_uid("r"), user_id, _image_bytes(8)))

    response = search_service.search(SearchRequest(query="image", user_id=user_id, top_k=10))

    assert response.data
    assert any(evidence.content.startswith("image ") for evidence in response.data)


def test_image_query_retrieves_similar_image(
    add_service: AddService, search_service: SearchService
) -> None:
    """图片查询能通过图片向量通道检索到相似图片记忆。"""
    user_id = _uid("u")
    data = _image_bytes(8)
    add_service.add(_image_request(_uid("r"), user_id, data))

    query = [{"type": "image_url", "image_url": {"url": _data_url(data)}}]
    response = search_service.search(SearchRequest(query=query, user_id=user_id, top_k=10))

    assert response.data


def test_mixed_content_is_retrievable_by_text_and_image(
    add_service: AddService, search_service: SearchService
) -> None:
    """文本与图片混合消息同时具备文本与图片检索能力。"""
    user_id = _uid("u")
    data = _image_bytes(10)
    request = AddRequest(
        request_id=_uid("r"),
        user_id=user_id,
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a red square photo"},
                    {"type": "image_url", "image_url": {"url": _data_url(data)}},
                ],
            }
        ],
    )
    add_service.add(request)

    by_text = search_service.search(SearchRequest(query="square", user_id=user_id, top_k=10))
    assert by_text.data

    by_image = search_service.search(
        SearchRequest(
            query=[{"type": "image_url", "image_url": {"url": _data_url(data)}}],
            user_id=user_id,
            top_k=10,
        )
    )
    assert by_image.data


def test_top_k_100_is_accepted(add_service: AddService, search_service: SearchService) -> None:
    """top_k=100 被接受且返回数量不超过上限。"""
    user_id = _uid("u")
    for index in range(3):
        add_service.add(_text_request(_uid("r"), user_id, f"memory number {index}"))

    response = search_service.search(SearchRequest(query="memory", user_id=user_id, top_k=100))

    assert len(response.data) <= 100
    assert response.data


def test_search_returns_nothing_for_unknown_user(search_service: SearchService) -> None:
    """无记忆的用户得到空结果。"""
    response = search_service.search(
        SearchRequest(query="nothing here", user_id=_uid("u"), top_k=10)
    )
    assert response.data == []


def test_search_via_http(client: TestClient) -> None:
    """官方 /search 路由端到端可用。"""
    user_id = _uid("u")
    headers = {"X-Api-Key": "test-key"}
    add_payload = {
        "request_id": _uid("r"),
        "user_id": user_id,
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "hello memory baseline"}],
    }
    assert client.post("/add", json=add_payload, headers=headers).status_code == 200

    response = client.post(
        "/search",
        json={"query": "baseline", "user_id": user_id, "top_k": 5},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    assert body["data"]
    assert all({"id", "content"} <= set(item) for item in body["data"])


def test_search_requires_auth(client: TestClient) -> None:
    """缺少认证时 /search 返回 401。"""
    response = client.post("/search", json={"query": "x", "user_id": _uid("u"), "top_k": 5})
    assert response.status_code == 401
