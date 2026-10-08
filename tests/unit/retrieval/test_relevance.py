"""Strong-anchor relevance gate tests."""

from uuid import uuid4

import pytest

from masm.retrieval.baseline import (
    IMAGE_VECTOR_CHANNEL,
    LEXICAL_CHANNEL,
    METADATA_CHANNEL,
    TEXT_VECTOR_CHANNEL,
)
from masm.retrieval.relevance import RelevanceGate
from masm.storage.types import MemoryCandidate


def _candidate(**signals: float) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=uuid4(),
        user_id="user-1",
        content="evidence",
        score=0.01,
        retrieval_signals=signals,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.01, 1.01])
def test_similarity_thresholds_must_be_finite_cosine_values(value: float) -> None:
    with pytest.raises(ValueError):
        RelevanceGate(min_text_similarity=value)
    with pytest.raises(ValueError):
        RelevanceGate(min_image_similarity=value)


def test_lexical_presence_is_a_strong_anchor_even_with_low_rank() -> None:
    candidate = _candidate(**{LEXICAL_CHANNEL: 0.0})

    assert RelevanceGate().filter([candidate]) == [candidate]


def test_text_and_image_vectors_use_independent_thresholds() -> None:
    text = _candidate(**{TEXT_VECTOR_CHANNEL: 0.48})
    image = _candidate(**{IMAGE_VECTOR_CHANNEL: 0.42})
    weak_text = _candidate(**{TEXT_VECTOR_CHANNEL: 0.479})
    weak_image = _candidate(**{IMAGE_VECTOR_CHANNEL: 0.419})

    assert RelevanceGate().filter([text, weak_text, image, weak_image]) == [text, image]


def test_metadata_and_fused_rank_alone_do_not_make_a_strong_anchor() -> None:
    metadata = _candidate(**{METADATA_CHANNEL: 1.0})
    no_signals = _candidate()

    assert RelevanceGate().filter([metadata, no_signals]) == []
