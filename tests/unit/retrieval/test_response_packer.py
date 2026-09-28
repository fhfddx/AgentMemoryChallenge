"""响应打包单元测试：按实际序列化字节数裁剪，且只保留完整证据。"""

from uuid import uuid4

import pytest  # noqa: I001

from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.response_packer import ResponsePacker, resolve_max_response_bytes

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


def test_result_is_a_strict_prefix_of_the_original_ranking() -> None:
    items = [_evidence(f"memory {index}", index) for index in range(1, 21)]

    kept = _packer(max_bytes=400).pack(items, top_k=100)

    expected = [item.memory_id for item in items][: len(kept)]
    assert [item.memory_id for item in kept] == expected
    assert len(kept) < len(items)


def test_evidence_is_never_truncated() -> None:
    """被保留的证据内容必须逐字完整。"""
    items = [_evidence("x" * 200, 1), _evidence("y" * 200, 2), _evidence("z" * 200, 3)]

    kept = _packer(max_bytes=900).pack(items, top_k=100)

    assert kept
    for item in kept:
        assert item.content in {"x" * 200, "y" * 200, "z" * 200}
        assert len(item.content) == 200


def test_oversized_first_evidence_returns_nothing() -> None:
    """第一条证据本身就超过限制时，不得返回半条证据。"""
    items = [_evidence("x" * 5000, 1), _evidence("small", 2)]

    kept = _packer(max_bytes=500).pack(items, top_k=100)

    assert kept == []


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


def test_empty_input_returns_empty() -> None:
    assert _packer().pack([], top_k=100) == []
