"""响应打包单元测试：按实际序列化字节数裁剪，且只保留完整证据。"""

from uuid import uuid4

import pytest  # noqa: I001

from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.response_packer import (
    ResponsePacker,
    minimum_response_bytes,
    resolve_max_response_bytes,
)
from masm.schemas.api import SearchResponse

_MAX_BYTES = 30 * 1024 * 1024


def _evidence(content: str, rank: int) -> RankedEvidence:
    return RankedEvidence(
        memory_id=uuid4(), user_id="user-1", content=content, score=1.0, rank=rank
    )


def _packer(max_bytes: int = _MAX_BYTES) -> ResponsePacker:
    return ResponsePacker(max_bytes=max_bytes)


def test_small_response_is_returned_unchanged() -> None:
    items = [_evidence(f"memory {index}", index) for index in range(1, 4)]

    kept = _packer().pack(items, top_k=100)

    assert [item.memory_id for item in kept] == [item.memory_id for item in items]


def test_top_k_limits_the_result() -> None:
    items = [_evidence(f"memory {index}", index) for index in range(1, 11)]

    kept = _packer().pack(items, top_k=3)

    assert len(kept) == 3


def test_kept_items_preserve_relative_rank_after_skipping_oversized_evidence() -> None:
    items = [_evidence("first", 1), _evidence("x" * 5000, 2), _evidence("third", 3)]

    kept = _packer(max_bytes=400).pack(items, top_k=100)

    assert [item.memory_id for item in kept] == [items[0].memory_id, items[2].memory_id]


def test_evidence_is_never_truncated() -> None:
    """被保留的证据内容必须逐字完整。"""
    items = [_evidence("x" * 200, 1), _evidence("y" * 200, 2), _evidence("z" * 200, 3)]

    kept = _packer(max_bytes=900).pack(items, top_k=100)

    assert kept
    for item in kept:
        assert item.content in {"x" * 200, "y" * 200, "z" * 200}
        assert len(item.content) == 200


def test_oversized_first_evidence_does_not_hide_shorter_evidence() -> None:
    """超长父级证据不能阻止后续短证据返回；内容仍不得截断。"""
    items = [_evidence("x" * 5000, 1), _evidence("small", 2)]

    kept = _packer(max_bytes=500).pack(items, top_k=100)

    assert [item.content for item in kept] == ["small"]


def test_top_k_100_with_large_content_stays_within_limit() -> None:
    items = [_evidence("a" * 1024, index) for index in range(1, 101)]

    kept = _packer(max_bytes=30 * 1024).pack(items, top_k=100)

    assert len(kept) <= 100
    # 用实际序列化字节数复核上限。
    assert _serialized_bytes(kept) <= 30 * 1024


def _serialized_bytes(items) -> int:
    from masm.schemas.api import MemoryEvidence, SearchResponse

    return len(
        SearchResponse(
            data=[
                MemoryEvidence(id=str(item.memory_id), content=item.content, score=item.score)
                for item in items
            ]
        ).model_dump_json()
    )


@pytest.mark.parametrize("configured", [30 * 1024 * 1024 + 1, 10**9])
def test_configured_limit_cannot_exceed_hard_cap(configured: int) -> None:
    assert resolve_max_response_bytes(configured) == _MAX_BYTES


def test_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_max_response_bytes(-1)


def test_limit_below_serializable_minimum_is_rejected() -> None:
    """小于空 SearchResponse 实际序列化字节数的上限必须被拒绝。

    否则任何 Search 都会退化为恒定空结果，且无法与「确实没有命中」区分。
    """
    minimum = minimum_response_bytes()
    assert minimum > 0
    for configured in (0, 1, minimum - 1):
        with pytest.raises(ValueError):
            resolve_max_response_bytes(configured)


def test_limit_at_serializable_minimum_is_accepted() -> None:
    """恰好等于空响应字节数的上限必须合法：它仍然允许正确表达空结果。"""
    minimum = minimum_response_bytes()

    assert resolve_max_response_bytes(minimum) == minimum
    assert ResponsePacker(max_bytes=minimum).max_bytes == minimum
    assert ResponsePacker(max_bytes=minimum).pack([_evidence("x", 1)], top_k=100) == []


def test_minimum_is_derived_from_the_actual_response_schema() -> None:
    """下限必须取自真实序列化结果，而不是写死的字节数。"""
    expected = len(SearchResponse(data=[]).model_dump_json().encode("utf-8"))

    assert minimum_response_bytes() == expected


def test_empty_input_returns_empty() -> None:
    assert _packer().pack([], top_k=100) == []
