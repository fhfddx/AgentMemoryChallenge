"""The answer-extraction judge: refusals come from null or ungrounded answers, never from errors."""

import pytest
from pydantic import ValidationError

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_judge import (
    JUDGE_PROMPT_VERSION,
    EvidenceSufficiencyJudge,
    ExtractedAnswer,
)


def test_grounded_answer_keeps_the_evidence() -> None:
    llm = FakeStructuredLLM([{"answer": "drawer three"}], model="gpt-4o-mini")
    judge = EvidenceSufficiencyJudge(llm)

    outcome = judge.judge(
        "Where did I store the amber key?",
        ("I stored the amber key in drawer three.",),
    )

    assert outcome.supported is True
    assert outcome.failure_category == "none"
    assert [request.prompt_version for request in llm.requests] == [JUDGE_PROMPT_VERSION]


def test_null_answer_withholds_the_evidence() -> None:
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([{"answer": None}]))

    outcome = judge.judge(
        "Where did I store the amber key?",
        ("The amber key matters to me.",),
    )

    assert outcome.supported is False
    assert outcome.failure_category == "none"


def test_answer_outside_the_evidence_withholds_the_evidence() -> None:
    # 模型只能在证据外编造答案时，说明证据没有陈述所求事实。
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([{"answer": "in the blue box"}]))

    outcome = judge.judge(
        "Where did I store the amber key?",
        ("The amber key matters to me.",),
    )

    assert outcome.supported is False


def test_provider_failure_keeps_the_evidence() -> None:
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([]))

    outcome = judge.judge(
        "Where did I store the amber key?",
        ("The amber key matters to me.",),
    )

    assert outcome.supported is True
    assert outcome.failure_category == "judge_unavailable"


def test_empty_evidence_withholds_without_a_call() -> None:
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([]))

    outcome = judge.judge("Where did I store the amber key?", ())

    assert outcome.supported is False
    assert outcome.failure_category == "none"


def test_schema_rejects_extra_fields_and_non_strings() -> None:
    with pytest.raises(ValidationError):
        ExtractedAnswer.model_validate({"answer": "drawer three", "confidence": 0.9})
    with pytest.raises(ValidationError):
        ExtractedAnswer.model_validate({"answer": 42})
