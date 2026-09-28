"""基线检索的跨用户隔离测试（发布阻断项）。

两个用户拥有完全相同的内容，因此全文、文本向量、图片向量与元数据通道都会产生
高度相似甚至完全相同的候选；所有检索路径仍只能返回目标用户的证据。
"""

import base64
import io
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import select

from masm.schemas.api import AddRequest, SearchRequest
from masm.services.add_service import AddService
from masm.services.search_service import SearchService
from masm.storage.db import Database
from masm.storage.models import Memory


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _image_bytes(size: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (10, 200, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url(data: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


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
                "content": [
                    {"type": "text", "text": "the same shared photo"},
                    {"type": "image_url", "image_url": {"url": _data_url(data)}},
                ],
            }
        ],
    )


def _owners(database: Database, memory_ids: list[str]) -> set[str]:
    with database.session() as session:
        rows = session.execute(
            select(Memory.user_id).where(Memory.id.in_([UUID(value) for value in memory_ids]))
        ).scalars()
        return set(rows)


def _seed_two_identical_users(
    add_service: AddService, database: Database
) -> tuple[str, str, str, bytes]:
    user_a, user_b = _uid("user-a"), _uid("user-b")
    text = "identical shared memory about the same green square"
    data = _image_bytes(8)

    for user_id in (user_a, user_b):
        add_service.add(_text_request(_uid("r"), user_id, text))
        add_service.add(_image_request(_uid("r"), user_id, data))

    return user_a, user_b, text, data


def test_text_channel_isolated_between_users(
    add_service: AddService, search_service: SearchService, database: Database
) -> None:
    """全文与文本向量通道只返回目标用户证据。"""
    user_a, _user_b, text, _data = _seed_two_identical_users(add_service, database)

    response = search_service.search(SearchRequest(query=text, user_id=user_a, top_k=100))

    assert response.data
    memory_ids = [evidence.id for evidence in response.data]
    assert _owners(database, memory_ids) == {user_a}


def test_image_channel_isolated_between_users(
    add_service: AddService, search_service: SearchService, database: Database
) -> None:
    """图片向量通道只返回目标用户证据。"""
    user_a, _user_b, _text, data = _seed_two_identical_users(add_service, database)

    response = search_service.search(
        SearchRequest(
            query=[{"type": "image_url", "image_url": {"url": _data_url(data)}}],
            user_id=user_a,
            top_k=100,
        )
    )

    assert response.data
    memory_ids = [evidence.id for evidence in response.data]
    assert _owners(database, memory_ids) == {user_a}


def test_metadata_channel_isolated_between_users(
    add_service: AddService, search_service: SearchService, database: Database
) -> None:
    """元数据（模态）通道只返回目标用户证据。"""
    user_a, _user_b, _text, data = _seed_two_identical_users(add_service, database)

    response = search_service.search(
        SearchRequest(
            query=[{"type": "image_url", "image_url": {"url": _data_url(data)}}],
            user_id=user_a,
            top_k=100,
        )
    )

    assert response.data
    memory_ids = [evidence.id for evidence in response.data]
    assert _owners(database, memory_ids) == {user_a}


def test_top_k_100_still_isolated(
    add_service: AddService, search_service: SearchService, database: Database
) -> None:
    """top_k=100 时仍不跨用户泄漏。"""
    user_a, _user_b, text, _data = _seed_two_identical_users(add_service, database)

    response = search_service.search(SearchRequest(query=text, user_id=user_a, top_k=100))

    memory_ids = [evidence.id for evidence in response.data]
    assert memory_ids
    assert _owners(database, memory_ids) == {user_a}
