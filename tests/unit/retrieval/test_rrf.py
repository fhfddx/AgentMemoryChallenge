"""加权倒数排名融合（RRF）单元测试。"""

from uuid import UUID, uuid4

import pytest

from masm.retrieval.rrf import RankedChannel, reciprocal_rank_fusion
from masm.storage.types import MemoryCandidate


def _candidate(memory_id: UUID, user_id: str = "user-1", content: str = "") -> MemoryCandidate:
    return MemoryCandidate(memory_id=memory_id, user_id=user_id, content=content, score=0.0)


def test_single_channel_scores_follow_reciprocal_rank() -> None:
    """单通道分数为 weight / (k + rank)。"""
    ids = [uuid4(), uuid4(), uuid4()]
    channel = RankedChannel("lexical", [_candidate(memory_id) for memory_id in ids])

    fused = reciprocal_rank_fusion([channel], {"lexical": 1.0}, k=60)

    assert [item.memory_id for item in fused] == ids
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[1].score == pytest.approx(1 / 62)
    assert fused[2].score == pytest.approx(1 / 63)


def test_scores_sum_across_channels() -> None:
    """同一记忆在多通道命中时分数累加。"""
    shared, other = uuid4(), uuid4()
    lexical = RankedChannel("lexical", [_candidate(shared), _candidate(other)])
    vector = RankedChannel("text_vector", [_candidate(shared)])

    fused = reciprocal_rank_fusion(
        [lexical, vector], {"lexical": 1.0, "text_vector": 2.0}, k=60
    )
    scores = {item.memory_id: item.score for item in fused}

    assert scores[shared] == pytest.approx(1 / 61 + 2 / 61)
    assert scores[other] == pytest.approx(1 / 62)
    assert fused[0].memory_id == shared


def test_weights_scale_channel_contribution() -> None:
    """通道权重决定排序。"""
    lexical_best, vector_best = uuid4(), uuid4()
    lexical = RankedChannel("lexical", [_candidate(lexical_best)])
    vector = RankedChannel("text_vector", [_candidate(vector_best)])

    fused = reciprocal_rank_fusion(
        [lexical, vector], {"lexical": 1.0, "text_vector": 10.0}, k=60
    )

    assert [item.memory_id for item in fused] == [vector_best, lexical_best]


@pytest.mark.parametrize("weights", [{"lexical": 0.0}, {}])
def test_channels_without_positive_weight_are_ignored(weights: dict) -> None:
    """权重为 0 或缺失的通道不参与融合。"""
    channel = RankedChannel("lexical", [_candidate(uuid4())])
    assert reciprocal_rank_fusion([channel], weights) == []


def test_channel_ranks_are_recorded_per_channel() -> None:
    """融合结果记录每个通道内的名次。"""
    memory_id = uuid4()
    fused = reciprocal_rank_fusion(
        [
            RankedChannel("lexical", [_candidate(memory_id)]),
            RankedChannel("text_vector", [_candidate(memory_id)]),
        ],
        {"lexical": 1.0, "text_vector": 1.0},
    )
    assert fused[0].channel_ranks == {"lexical": 1, "text_vector": 1}


def test_duplicate_candidate_in_same_channel_counted_once() -> None:
    """同一通道内重复候选只计一次（取最佳名次）。"""
    memory_id = uuid4()
    channel = RankedChannel("lexical", [_candidate(memory_id), _candidate(memory_id)])

    fused = reciprocal_rank_fusion([channel], {"lexical": 1.0}, k=60)

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[0].channel_ranks == {"lexical": 1}


def test_empty_rankings_return_empty_list() -> None:
    assert reciprocal_rank_fusion([], {"lexical": 1.0}) == []


def test_k_parameter_is_applied() -> None:
    memory_id = uuid4()
    fused = reciprocal_rank_fusion(
        [RankedChannel("lexical", [_candidate(memory_id)])], {"lexical": 1.0}, k=0
    )
    assert fused[0].score == pytest.approx(1.0)


def test_tie_is_broken_deterministically_by_memory_id() -> None:
    """同分时按 memory_id 稳定排序。"""
    low, high = UUID(int=1), UUID(int=2)
    channels = [
        RankedChannel("lexical", [_candidate(high)]),
        RankedChannel("text_vector", [_candidate(low)]),
    ]

    fused = reciprocal_rank_fusion(channels, {"lexical": 1.0, "text_vector": 1.0})

    assert [item.memory_id for item in fused] == [low, high]
