"""官方 Add / Search Schema 契约测试。"""

import pytest
from pydantic import ValidationError

from masm.schemas.api import AddRequest, SearchRequest
from masm.schemas.content import ImageURLPart, TextPart


def _text(text: str) -> dict[str, str]:
    """构造一个文本内容分片。"""
    return {"type": "text", "text": text}


def _image() -> dict[str, object]:
    """构造一个图片内容分片。"""
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="},
    }


def test_add_request_preserves_mixed_content_order() -> None:
    """混合内容分片的原始顺序在解析后保持不变。"""
    request = AddRequest(
        request_id="req-1",
        user_id="user-1",
        session_id="session-1",
        messages=[
            {
                "role": "user",
                "content": [_text("第一段"), _image(), _text("第二段"), _image(), _text("第三段")],
            }
        ],
    )
    content = request.messages[0].content
    assert isinstance(content, list)
    assert [part.type for part in content] == ["text", "image_url", "text", "image_url", "text"]
    texts = [part.text for part in content if isinstance(part, TextPart)]
    assert texts == ["第一段", "第二段", "第三段"]


def test_search_accepts_top_k_100() -> None:
    """Search 接受 top_k=100，拒绝超出上限的 top_k=101。"""
    request = SearchRequest(query="查询", user_id="user-1", top_k=100)
    assert request.top_k == 100

    with pytest.raises(ValidationError):
        SearchRequest(query="查询", user_id="user-1", top_k=101)


def test_remote_image_url_is_rejected() -> None:
    """图片只接受 data image URL，远程 http(s) URL 被拒绝。"""
    with pytest.raises(ValidationError):
        ImageURLPart(image_url={"url": "https://example.com/picture.png"})

    part = ImageURLPart(image_url={"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="})
    assert part.image_url.url.startswith("data:image/")


def test_message_accepts_plain_text_content() -> None:
    """Message.content 接受纯文本字符串。"""
    request = AddRequest(
        request_id="req-2",
        user_id="user-1",
        session_id="session-1",
        messages=[{"role": "assistant", "content": "纯文本回复"}],
    )
    assert request.messages[0].content == "纯文本回复"


def test_search_accepts_mixed_content_query() -> None:
    """SearchRequest.query 接受保持顺序的内容数组。"""
    request = SearchRequest(query=[_text("找这张图"), _image()], user_id="user-1", top_k=10)
    assert isinstance(request.query, list)
    assert request.query[0].type == "text"
