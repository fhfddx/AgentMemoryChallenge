"""The sufficiency judge must only ever withhold evidence on a strict negative verdict."""

import pytest
from pydantic import ValidationError

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_judge import (
    JUDGE_PROMPT_VERSION,
    EvidenceSufficiencyJudge,
    SufficiencyVerdict,
)


def test_judge_returns_the_strict_verdict() -> None:
    llm = FakeStructuredLLM([{"answers": False}], model="gpt-4o-mini")
    judge = EvidenceSufficiencyJudge(llm)

    outcome = judge.judge("Where did I store the amber key?", ("The amber key matters.",))

    assert outcome.answers is False
    assert outcome.failure_category == "none"
    assert [request.prompt_version for request in llm.requests] == [JUDGE_PROMPT_VERSION]


def test_judge_withholds_nothing_when_the_provider_fails() -> None:
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([]))

    outcome = judge.judge("Where did I store the amber key?", ("The amber key matters.",))

    assert outcome.answers is False
    assert outcome.failure_category == "judge_unavailable"


def test_judge_withholds_nothing_without_evidence() -> None:
    judge = EvidenceSufficiencyJudge(FakeStructuredLLM([]))

    outcome = judge.judge("Where did I store the amber key?", ())

    assert outcome.answers is False
    assert outcome.failure_category == "none"


def test_verdict_schema_rejects_extra_fields_and_non_booleans() -> None:
    with pytest.raises(ValidationError):
        SufficiencyVerdict.model_validate({"answers": True, "rationale": "because"})
    with pytest.raises(ValidationError):
        SufficiencyVerdict.model_validate({"answers": "yes"})
