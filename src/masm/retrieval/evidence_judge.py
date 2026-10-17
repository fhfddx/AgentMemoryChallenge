"""Sufficiency judge: decide whether the selected evidence actually answers the question.

The planner decides *what* the question asks and *how* candidates connect; its incidental
"insufficient" verdict is not a reliable answerability judgement (an official run showed
it withholding correct answers on atomic questions). This judge asks one narrow binary
question about the already selected evidence, and is only consulted when the planner's
verdict is not sufficient, so confident searches pay nothing.

A judge that is unavailable, fails, or returns anything but a strict ``{"answers": bool}``
never withholds evidence: the safe default is to keep the additive result.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, StrictBool

from masm.providers.llm import ModelRequest, StructuredLLM

JUDGE_PROMPT_VERSION = "evidence-sufficiency-judge-v1"
MAX_EVIDENCE_CHARS = 600

PROMPT = (
    "You decide whether the provided evidence answers the question. Return JSON with "
    "exactly one boolean field named answers. Set answers to true only when the evidence "
    "states the requested fact for the exact requested subject; set it to false when the "
    "evidence merely mentions related entities, a different subject, or a different "
    "attribute. Do not answer the question, do not choose an option, and do not output any "
    "other field."
)


class SufficiencyVerdict(BaseModel):
    """The judge output: a single strict boolean."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answers: StrictBool


@dataclass(frozen=True)
class JudgeOutcome:
    """Judge result with an explicit failure mode instead of a silent default."""

    answers: bool
    failure_category: str = "none"


class EvidenceSufficiencyJudge:
    """One narrow binary call over already selected evidence."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds

    def judge(self, question: str, evidence: tuple[str, ...]) -> JudgeOutcome:
        if not evidence:
            return JudgeOutcome(False)
        rendered = "\n---\n".join(item[:MAX_EVIDENCE_CHARS] for item in evidence)
        try:
            verdict = self._llm.complete_json(
                ModelRequest(
                    prompt=PROMPT,
                    payload={"question": question, "evidence": rendered},
                    model=self._llm.model,
                    prompt_version=JUDGE_PROMPT_VERSION,
                    timeout_seconds=self._timeout_seconds,
                    max_attempts=1,
                    temperature=0.0,
                ),
                SufficiencyVerdict,
            )
        except Exception:
            return JudgeOutcome(False, "judge_unavailable")
        if not isinstance(verdict, SufficiencyVerdict):
            return JudgeOutcome(False, "judge_invalid")
        return JudgeOutcome(bool(verdict.answers))
