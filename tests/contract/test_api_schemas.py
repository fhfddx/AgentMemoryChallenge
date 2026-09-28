"""官方 Add / Search Schema 契约测试。"""

import pytest
from pydantic import ValidationError

from masm.schemas.api import (
    AddRequest,
    AddResponse,
    MemoryEvidence,
    SearchRequest,
    SearchResponse,
)
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


def test_add_request_accepts_many_messages() -> None:
    """多模态官方契约未承诺 messages 上限，不得强加 max_length。"""
    messages = [{"role": "user", "content": f"消息{i}"} for i in range(25)]
    request = AddRequest(
        request_id="req-many",
        user_id="user-1",
        session_id="session-1",
        messages=messages,
    )
    assert len(request.messages) == 25


def test_search_options_must_be_non_empty_and_non_blank() -> None:
    """options 可选；若提供则数组非空且每项非空白。"""
    assert SearchRequest(query="q", user_id="user-1", top_k=10).options is None

    request = SearchRequest(query="q", user_id="user-1", options=["选项A", "选项B"], top_k=10)
    assert request.options == ["选项A", "选项B"]

    with pytest.raises(ValidationError):
        SearchRequest(query="q", user_id="user-1", options=[], top_k=10)
    with pytest.raises(ValidationError):
        SearchRequest(query="q", user_id="user-1", options=["ok", "   "], top_k=10)
    with pytest.raises(ValidationError):
        SearchRequest(query="q", user_id="user-1", options=[""], top_k=10)


def test_text_part_rejects_blank_text() -> None:
    """TextPart.text 必须拒绝空字符串和纯空格字符串。"""
    with pytest.raises(ValidationError):
        TextPart(text="")
    with pytest.raises(ValidationError):
        TextPart(text="   ")
    assert TextPart(text="hello").text == "hello"


def test_add_response_rejects_false() -> None:
    """AddResponse.success 只能接受布尔值 true。"""
    with pytest.raises(ValidationError):
        AddResponse(success=False, request_id="r", user_id="u", session_id="s")

    response = AddResponse(success=True, request_id="r", user_id="u", session_id="s")
    assert response.success is True


def test_add_response_forbids_extra_fields() -> None:
    """AddResponse 不允许额外的响应字段。"""
    with pytest.raises(ValidationError):
        AddResponse(success=True, request_id="r", user_id="u", session_id="s", answer="泄露")


def test_memory_evidence_content_must_be_non_empty() -> None:
    """MemoryEvidence.content 必须非空。"""
    with pytest.raises(ValidationError):
        MemoryEvidence(id="m1", content="")
    with pytest.raises(ValidationError):
        MemoryEvidence(id="m1", content="   ")
    with pytest.raises(ValidationError):
        MemoryEvidence(id="m1", content=[])


def test_memory_evidence_accepts_optional_score_and_created_at() -> None:
    """MemoryEvidence 接受官方允许的可选字段 score 与 created_at。"""
    evidence = MemoryEvidence(
        id="m1",
        content="文本证据",
        score=0.95,
        created_at="2026-09-28T00:00:00Z",
    )
    assert evidence.score == 0.95
    assert evidence.created_at == "2026-09-28T00:00:00Z"

    minimal = MemoryEvidence(id="m2", content="文本证据")
    assert minimal.score is None
    assert minimal.created_at is None


def test_memory_evidence_forbids_extra_fields() -> None:
    """MemoryEvidence 不允许额外字段，防止泄露最终答案等内容。"""
    with pytest.raises(ValidationError):
        MemoryEvidence(id="m1", content="文本证据", final_answer="答案")


def test_search_response_wraps_evidence() -> None:
    """SearchResponse 只包含 data 证据数组。"""
    response = SearchResponse(data=[MemoryEvidence(id="m1", content="证据", score=0.8)])
    assert len(response.data) == 1
    assert response.data[0].id == "m1"
    assert response.data[0].score == 0.8


def test_search_response_allows_empty_data() -> None:
    """无结果时 SearchResponse.data 允许为空数组。"""
    response = SearchResponse(data=[])
    assert response.data == []
