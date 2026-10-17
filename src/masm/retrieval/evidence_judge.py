"""Answer-extraction judge: withhold evidence only when no answer can be copied out of it.

Earlier attempts asked a model to *judge* whether the selected evidence answers the question
(a yes/no verdict, then a support-span extraction). Both proved too lenient on shapes where the
evidence merely mentions related entities, and the planner's own insufficiency verdict withheld
correct answers. This judge asks a different, easier task and validates the result
deterministically:

- the model must *answer* the question using only the evidence and copy the answer verbatim;
- a grounded answer keeps the evidence;
- a null answer and an answer that cannot be grounded in the evidence are the two proven shapes
  of "the evidence does not state the requested fact", so both refuse;
- transport or schema failures keep the additive result, so a broken judge never withholds.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, StrictStr

from masm.providers.llm import ModelRequest, StructuredLLM
from masm.retrieval.evidence_graph_rules import grounded_quote

JUDGE_PROMPT_VERSION = "evidence-answer-judge-v1"
MAX_EVIDENCE_CHARS = 600

PROMPT = (
    "Answer the question using only the evidence. Copy the answer text verbatim from the "
    "evidence: no paraphrase, no explanation. Return JSON with exactly one field named "
    "answer holding that copied text, or null when the evidence does not state the requested "
    "fact for the exact requested subject (for example when it only mentions a related "
    "entity, a different subject, or a different attribute). Never guess and never use "
    "outside knowledge."
)


class ExtractedAnswer(BaseModel):
    """The judge output: a verbatim answer copied from the evidence, or null."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: StrictStr | None = None


@dataclass(frozen=True)
class JudgeOutcome:
    """Judge result: whether the evidence supports a grounded answer, plus failure mode."""

    supported: bool
    failure_category: str = "none"


class EvidenceSufficiencyJudge:
    """One answer-extraction call over already selected evidence."""

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
                ExtractedAnswer,
            )
        except Exception:
            return JudgeOutcome(True, "judge_unavailable")
        if not isinstance(verdict, ExtractedAnswer):
            return JudgeOutcome(True, "judge_invalid")
        answer = verdict.answer
        if answer is None or not answer.strip():
            return JudgeOutcome(False)
        if not any(grounded_quote(answer, item) for item in evidence):
            # 答案在证据里找不到原文：模型只能编造，视为"证据没有陈述所求事实"。
            return JudgeOutcome(False)
        return JudgeOutcome(True)
