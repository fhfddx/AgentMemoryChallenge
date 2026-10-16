"""Paired synthetic evidence-graph suite; no database, provider body or production data access.

The suite is fixed before any run: thirteen positive cases and their thirteen negative twins.
A run is eligible only when every positive and every negative case passes, no case falls back
to the frozen selector and no case reports a provider or schema failure. Passing this suite
proves deterministic evidence-graph behaviour only; it is not an official score.
"""

import argparse
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO
from uuid import UUID

import masm.providers.llm as llm_module
from masm.config import RuntimeProfile, Settings
from masm.providers.diagnostics import ProviderFailureDiagnostics
from masm.providers.errors import ProviderError
from masm.providers.llm import OpenAICompatibleLLM, StructuredLLM
from masm.retrieval.evidence_graph import (
    EvidenceGraphPlanner,
    GraphContext,
    GraphEvidenceSelector,
    GraphSelection,
)
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import RankedEvidence

_USER = "synthetic-evidence-graph"

# 仅用于固定合成案例的候选人字段；绝不包含真实用户数据。
Candidate = tuple[str, int, str]


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    candidates: tuple[Candidate, ...]
    positive: bool
    proposal: Mapping[str, object]
    expected_state: str
    required_fragments: tuple[str, ...] = ()
    forbidden_fragments: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    context_pairs: tuple[tuple[str, str], ...] = ()
    source_context: bool = False


def _requirement(quote: str, kind: str) -> dict[str, object]:
    return {"question_quote": quote, "kind": kind}


def _support(
    requirement_index: int, candidate_index: int, quote: str, role: str = "direct"
) -> dict[str, object]:
    return {
        "requirement_index": requirement_index,
        "candidate_index": candidate_index,
        "candidate_quote": quote,
        "role": role,
    }


def _link(left: int, right: int, left_quote: str, right_quote: str, kind: str) -> dict[str, object]:
    return {
        "left_candidate_index": left,
        "right_candidate_index": right,
        "left_quote": left_quote,
        "right_quote": right_quote,
        "kind": kind,
    }


def _proposal(
    requirements: Sequence[Mapping[str, object]],
    supports: Sequence[Mapping[str, object]],
    links: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return {"requirements": list(requirements), "supports": list(supports), "links": list(links)}


_AMBER_QUESTION = "Where did I store the amber key?"
_AMBER_FACT = "I stored the amber key in drawer three."
_PROJECT_QUESTION = "What is the project code name and when is the review?"
_CHAIN_QUESTION = "Which city hosts the workshop that Nora attended?"
_CHAIN_FIRST = "Nora attended the Zephyr workshop."
_CHAIN_SECOND = "The Zephyr workshop is held in Lisbon."
_CODE_QUESTION = "What is the check-in code and where is the venue?"
_MOVE_QUESTION = "Which city did I move to?"
_CORRECTION_QUESTION = "What color bicycle did I buy?"


def _crowded_candidates(count: int = 32) -> tuple[Candidate, ...]:
    return tuple((f"s{index}", 0, f"Unrelated note number {index}.") for index in range(count))


def _crowded_positive() -> Case:
    candidates = (*_crowded_candidates(30), ("s30", 0, _CHAIN_FIRST), ("s31", 0, _CHAIN_SECOND))
    return Case(
        name="crowded_candidates",
        question=_CHAIN_QUESTION,
        candidates=candidates,
        positive=True,
        proposal=_proposal(
            [
                _requirement("Which city hosts the workshop", "location"),
                _requirement("that Nora attended", "subject"),
            ],
            [
                _support(1, 30, _CHAIN_FIRST, "direct"),
                _support(0, 31, _CHAIN_SECOND, "direct"),
            ],
            [_link(30, 31, _CHAIN_FIRST, _CHAIN_SECOND, "shared_explicit_mention")],
        ),
        expected_state="sufficient",
        required_fragments=("Nora", "Lisbon"),
        forbidden_fragments=("Unrelated note number 0.",),
    )


def _crowded_negative() -> Case:
    return Case(
        name="crowded_candidates_negative",
        question=_CHAIN_QUESTION,
        candidates=_crowded_candidates(),
        positive=False,
        proposal=_proposal(
            [_requirement("Which city hosts the workshop", "location")],
            [_support(0, 0, "Unrelated note number 0.", "context")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("Unrelated note number 0.",),
    )


CASES: tuple[Case, ...] = (
    Case(
        name="direct_fact",
        question=_AMBER_QUESTION,
        candidates=(("s0", 0, _AMBER_FACT),),
        positive=True,
        proposal=_proposal(
            [_requirement("Where did I store the amber key", "location")],
            [_support(0, 0, _AMBER_FACT, "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("drawer three",),
    ),
    Case(
        name="direct_fact_negative",
        question=_AMBER_QUESTION,
        candidates=(("s0", 0, "The amber key matters to me."),),
        positive=False,
        proposal=_proposal(
            [_requirement("Where did I store the amber key", "location")],
            [_support(0, 0, "The amber key matters to me.", "context")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("drawer three",),
    ),
    Case(
        name="two_attributes",
        question=_PROJECT_QUESTION,
        candidates=(
            ("s0", 0, "The project code name is Zephyr."),
            ("s0", 1, "The project review date is April 18."),
        ),
        positive=True,
        proposal=_proposal(
            [
                _requirement("What is the project code name", "attribute"),
                _requirement("when is the review", "time"),
            ],
            [
                _support(0, 0, "The project code name is Zephyr.", "direct"),
                _support(1, 1, "The project review date is April 18.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "The project code name is Zephyr.",
                    "The project review date is April 18.",
                    "same_source_context",
                )
            ],
        ),
        expected_state="sufficient",
        required_fragments=("Zephyr", "April 18"),
        source_context=True,
    ),
    Case(
        name="two_attributes_negative",
        question=_PROJECT_QUESTION,
        candidates=(
            ("s0", 0, "The project code name is Zephyr."),
            ("s1", 0, "The project review date is April 18."),
        ),
        positive=False,
        proposal=_proposal(
            [
                _requirement("What is the project code name", "attribute"),
                _requirement("when is the review", "time"),
            ],
            [
                _support(0, 0, "The project code name is Zephyr.", "direct"),
                _support(1, 1, "The project review date is April 18.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "The project code name is Zephyr.",
                    "The project review date is April 18.",
                    "same_source_context",
                )
            ],
        ),
        expected_state="partial",
        forbidden_fragments=("April 18",),
        source_context=True,
    ),
    Case(
        name="cross_source_chain",
        question=_CHAIN_QUESTION,
        candidates=(("s0", 0, _CHAIN_FIRST), ("s1", 0, _CHAIN_SECOND)),
        positive=True,
        proposal=_proposal(
            [
                _requirement("Which city hosts the workshop", "location"),
                _requirement("that Nora attended", "subject"),
            ],
            [
                _support(1, 0, _CHAIN_FIRST, "direct"),
                _support(0, 1, _CHAIN_SECOND, "direct"),
            ],
            [_link(0, 1, _CHAIN_FIRST, _CHAIN_SECOND, "shared_explicit_mention")],
        ),
        expected_state="sufficient",
        required_fragments=("Nora", "Lisbon"),
    ),
    Case(
        name="cross_source_chain_negative",
        question=_CHAIN_QUESTION,
        candidates=(("s0", 0, "Nora planned a workshop."), ("s1", 0, "The workshop is in Lisbon.")),
        positive=False,
        proposal=_proposal(
            [
                _requirement("Which city hosts the workshop", "location"),
                _requirement("that Nora attended", "subject"),
            ],
            [
                _support(1, 0, "Nora planned a workshop.", "context"),
                _support(0, 1, "The workshop is in Lisbon.", "context"),
            ],
            [
                _link(
                    0,
                    1,
                    "Nora planned a workshop.",
                    "The workshop is in Lisbon.",
                    "shared_explicit_mention",
                )
            ],
        ),
        expected_state="insufficient",
        forbidden_fragments=("Lisbon",),
    ),
    Case(
        name="elliptical_confirmation",
        question=_MOVE_QUESTION,
        candidates=(("s0", 0, "Did you move to Porto?"), ("s0", 1, "Yes, last week.")),
        positive=True,
        proposal=_proposal(
            [_requirement("Which city did I move to", "location")],
            [
                _support(0, 0, "Did you move to Porto?", "context"),
                _support(0, 1, "Yes, last week.", "direct"),
            ],
            [_link(0, 1, "Did you move to Porto?", "Yes, last week.", "confirmation")],
        ),
        expected_state="sufficient",
        required_fragments=("Porto", "Yes, last week."),
        source_context=True,
    ),
    Case(
        name="elliptical_confirmation_negative",
        question=_MOVE_QUESTION,
        candidates=(("s0", 0, "Did you move to Porto?"), ("s0", 1, "Yes, last week.")),
        positive=False,
        proposal=_proposal(
            [_requirement("Which city did I move to", "location")],
            [
                _support(0, 0, "Did you move to Porto?", "context"),
                _support(0, 1, "Yes, last week.", "direct"),
            ],
            [_link(0, 1, "Did you move to Porto?", "Yes, last week.", "confirmation")],
        ),
        expected_state="sufficient",
        required_fragments=("Yes, last week.",),
        forbidden_fragments=("Porto",),
        source_context=False,
    ),
    Case(
        name="forward_correction",
        question=_CORRECTION_QUESTION,
        candidates=(
            ("s0", 0, "Did you buy the blue bicycle?"),
            ("s0", 1, "No, I bought the red one."),
        ),
        positive=True,
        proposal=_proposal(
            [_requirement("What color bicycle did I buy", "attribute")],
            [
                _support(0, 0, "Did you buy the blue bicycle?", "context"),
                _support(0, 1, "No, I bought the red one.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "Did you buy the blue bicycle?",
                    "No, I bought the red one.",
                    "correction",
                )
            ],
        ),
        expected_state="sufficient",
        required_fragments=("red", "blue"),
        source_context=True,
    ),
    Case(
        name="forward_correction_negative",
        question=_CORRECTION_QUESTION,
        candidates=(
            ("s0", 0, "Did you buy the blue bicycle?"),
            ("s0", 1, "No, I bought the red one."),
        ),
        positive=False,
        proposal=_proposal(
            [_requirement("What color bicycle did I buy", "attribute")],
            [
                _support(0, 0, "Did you buy the blue bicycle?", "context"),
                _support(0, 1, "No, I bought the red one.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "Did you buy the blue bicycle?",
                    "No, I bought the red one.",
                    "shared_explicit_mention",
                )
            ],
        ),
        expected_state="sufficient",
        required_fragments=("red",),
        forbidden_fragments=("blue",),
        source_context=True,
    ),
    Case(
        name="persisted_two_hop",
        question=_CODE_QUESTION,
        candidates=(
            ("s0", 0, "The check-in code is K7-42."),
            ("s1", 0, "The venue is the old cinema."),
        ),
        positive=True,
        proposal=_proposal(
            [
                _requirement("What is the check-in code", "attribute"),
                _requirement("where is the venue", "location"),
            ],
            [
                _support(0, 0, "The check-in code is K7-42.", "direct"),
                _support(1, 1, "The venue is the old cinema.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "The check-in code is K7-42.",
                    "The venue is the old cinema.",
                    "persisted_relation",
                )
            ],
        ),
        expected_state="sufficient",
        required_fragments=("K7-42", "old cinema"),
        context_pairs=(("s0", "s1"),),
    ),
    Case(
        name="persisted_two_hop_negative",
        question=_CODE_QUESTION,
        candidates=(
            ("s0", 0, "The check-in code is K7-42."),
            ("s1", 0, "The venue is the old cinema."),
        ),
        positive=False,
        proposal=_proposal(
            [
                _requirement("What is the check-in code", "attribute"),
                _requirement("where is the venue", "location"),
            ],
            [
                _support(0, 0, "The check-in code is K7-42.", "direct"),
                _support(1, 1, "The venue is the old cinema.", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "The check-in code is K7-42.",
                    "The venue is the old cinema.",
                    "persisted_relation",
                )
            ],
        ),
        expected_state="partial",
        forbidden_fragments=("old cinema",),
    ),
    Case(
        name="wrong_subject",
        question=_MOVE_QUESTION,
        candidates=(("s0", 0, "I moved to Porto last spring."),),
        positive=True,
        proposal=_proposal(
            [_requirement("Which city did I move to", "location")],
            [_support(0, 0, "I moved to Porto last spring.", "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("Porto",),
    ),
    Case(
        name="wrong_subject_negative",
        question=_MOVE_QUESTION,
        candidates=(
            ("s0", 0, "My colleague moved to Porto. I have not shared my own destination."),
        ),
        positive=False,
        proposal=_proposal(
            [_requirement("Which city did I move to", "location")],
            [_support(0, 0, "My colleague moved to Porto.", "bridge")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("Porto",),
    ),
    Case(
        name="disconnected_generic_mention",
        question="Which city hosts the project workshop?",
        candidates=(("s0", 0, "The project workshop is in Lisbon."),),
        positive=True,
        proposal=_proposal(
            [_requirement("Which city hosts the project workshop", "location")],
            [_support(0, 0, "The project workshop is in Lisbon.", "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("Lisbon",),
    ),
    Case(
        name="disconnected_generic_mention_negative",
        question="Which city hosts the project workshop?",
        candidates=(
            ("s0", 0, "The project workshop was planned."),
            ("s1", 0, "The workshop takes place every spring."),
        ),
        positive=False,
        proposal=_proposal(
            [_requirement("Which city hosts the project workshop", "location")],
            [
                _support(0, 0, "The project workshop was planned.", "context"),
                _support(0, 1, "The workshop takes place every spring.", "context"),
            ],
            [
                _link(
                    0,
                    1,
                    "The project workshop was planned.",
                    "The workshop takes place every spring.",
                    "shared_explicit_mention",
                )
            ],
        ),
        expected_state="insufficient",
        forbidden_fragments=("planned",),
    ),
    Case(
        name="missing_value",
        question="What is the storage code for the amber key?",
        candidates=(("s0", 0, "The storage code for the amber key is K7-42."),),
        positive=True,
        proposal=_proposal(
            [_requirement("What is the storage code", "attribute")],
            [_support(0, 0, "The storage code for the amber key is K7-42.", "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("K7-42",),
    ),
    Case(
        name="missing_value_negative",
        question="What is the storage code for the amber key?",
        candidates=(("s0", 0, _AMBER_FACT),),
        positive=False,
        proposal=_proposal(
            [_requirement("What is the storage code", "attribute")],
            [_support(0, 0, "the amber key", "context")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("drawer three",),
    ),
    Case(
        name="option_overlap",
        question=_AMBER_QUESTION,
        candidates=(("s0", 0, _AMBER_FACT),),
        positive=True,
        options=("drawer three", "shelf two"),
        proposal=_proposal(
            [_requirement("Where did I store the amber key", "location")],
            [_support(0, 0, _AMBER_FACT, "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("drawer three",),
    ),
    Case(
        name="option_overlap_negative",
        question=_AMBER_QUESTION,
        candidates=(("s0", 0, "I stored the amber key in the hallway cabinet."),),
        positive=False,
        options=("drawer three", "shelf two"),
        proposal=_proposal(
            [_requirement("Where did I store the amber key", "location")],
            [_support(0, 0, "drawer three", "direct")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("drawer three",),
    ),
    _crowded_positive(),
    _crowded_negative(),
    Case(
        name="chinese_direct",
        question="我把琥珀钥匙放在哪里了？",
        candidates=(("s0", 0, "我把琥珀钥匙放在第三个抽屉里。"),),
        positive=True,
        proposal=_proposal(
            [_requirement("我把琥珀钥匙放在哪里了", "location")],
            [_support(0, 0, "我把琥珀钥匙放在第三个抽屉里。", "direct")],
        ),
        expected_state="sufficient",
        required_fragments=("第三个抽屉",),
    ),
    Case(
        name="chinese_direct_negative",
        question="我把琥珀钥匙放在哪里了？",
        candidates=(("s0", 0, "琥珀钥匙对我很重要。"),),
        positive=False,
        proposal=_proposal(
            [_requirement("我把琥珀钥匙放在哪里了", "location")],
            [_support(0, 0, "琥珀钥匙对我很重要。", "context")],
        ),
        expected_state="insufficient",
        forbidden_fragments=("第三个抽屉",),
    ),
    Case(
        name="chinese_context",
        question="我报名的课程在哪个城市举办？",
        candidates=(("s0", 0, "我报名了陶艺工作坊。"), ("s1", 0, "陶艺工作坊在里斯本举办。")),
        positive=True,
        proposal=_proposal(
            [
                _requirement("在哪个城市举办", "location"),
                _requirement("我报名的课程", "subject"),
            ],
            [
                _support(1, 0, "我报名了陶艺工作坊。", "direct"),
                _support(0, 1, "陶艺工作坊在里斯本举办。", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "我报名了陶艺工作坊。",
                    "陶艺工作坊在里斯本举办。",
                    "shared_explicit_mention",
                )
            ],
        ),
        expected_state="sufficient",
        required_fragments=("陶艺", "里斯本"),
    ),
    Case(
        name="chinese_context_negative",
        question="我报名的课程，工作坊在哪个城市举办？",
        candidates=(("s0", 0, "我报名了一个工作坊。"), ("s1", 0, "这个工作坊在里斯本举办。")),
        positive=False,
        proposal=_proposal(
            [
                _requirement("工作坊在哪个城市举办", "location"),
                _requirement("我报名的课程", "subject"),
            ],
            [
                _support(1, 0, "我报名了一个工作坊。", "direct"),
                _support(0, 1, "这个工作坊在里斯本举办。", "direct"),
            ],
            [
                _link(
                    0,
                    1,
                    "我报名了一个工作坊。",
                    "这个工作坊在里斯本举办。",
                    "shared_explicit_mention",
                )
            ],
        ),
        expected_state="partial",
        forbidden_fragments=("里斯本",),
    ),
)


def proposal_responses(reverse: bool = False) -> list[Mapping[str, object]]:
    """按案例顺序返回脚本化 proposal，供确定性 Fake Provider 使用。"""
    ordered = list(reversed(CASES)) if reverse else list(CASES)
    return [dict(case.proposal) for case in ordered]


def _ranked(case: Case) -> list[RankedEvidence]:
    return [
        RankedEvidence(
            memory_id=UUID(int=index + 1),
            user_id=_USER,
            content=text,
            score=1.0 / (index + 1),
            rank=index + 1,
            request_id=source,
            granularity="message",
            source_position=position,
        )
        for index, (source, position, text) in enumerate(case.candidates)
    ]


def _evidence_text(evidence: Sequence[RankedEvidence]) -> str:
    return "\n".join(str(item.source_context or item.content) for item in evidence)


def _passed(case: Case, outcome: GraphSelection) -> bool:
    selection = outcome.selection
    graph = outcome.graph
    if graph.fallback or graph.failure_category != "none":
        return False
    if selection.evidence_state != case.expected_state:
        return False
    text = _evidence_text(selection.evidence)
    if any(fragment not in text for fragment in case.required_fragments):
        return False
    return not any(fragment in text for fragment in case.forbidden_fragments)


def run_provider_check(llm: StructuredLLM, output: TextIO) -> int:
    """Run one fixed planner call and render only allowlisted Provider metadata."""
    captured: list[ProviderFailureDiagnostics] = []

    def capture_failure(value: ProviderFailureDiagnostics) -> None:
        captured.append(value)

    original_emit = llm_module.emit_provider_failure
    llm_module.emit_provider_failure = capture_failure
    try:
        case = CASES[0]
        EvidenceGraphPlanner(llm).plan(
            case.question,
            False,
            case.options,
            _ranked(case),
        )
    except ProviderError:
        if not captured:
            payload: dict[str, str | int] = {
                "case": "provider_check",
                "category": "unavailable_unknown",
            }
        else:
            diagnostic = captured[-1]
            payload = {
                "case": "provider_check",
                "category": diagnostic.category.value,
                "attempt": diagnostic.attempt,
            }
            if diagnostic.status_code is not None:
                payload["status_code"] = diagnostic.status_code
        print(json.dumps(payload, sort_keys=True), file=output, flush=True)
        return 1
    except Exception:
        print(
            json.dumps(
                {"case": "provider_check", "category": "internal_error"},
                sort_keys=True,
            ),
            file=output,
            flush=True,
        )
        return 2
    finally:
        llm_module.emit_provider_failure = original_emit
    print(
        json.dumps(
            {"case": "provider_check", "category": "success"},
            sort_keys=True,
        ),
        file=output,
        flush=True,
    )
    return 0


def run_probe(llm: StructuredLLM, output: TextIO, *, reverse: bool = False) -> int:
    """执行固定套件；只有全部正负例通过且零回退时才返回 0。"""
    selector = GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        EvidenceSelector(llm),
    )
    cases = list(reversed(CASES)) if reverse else list(CASES)
    passed = 0
    positive_passed = 0
    negative_passed = 0
    positive_total = sum(1 for case in cases if case.positive)
    negative_total = sum(1 for case in cases if not case.positive)
    fallbacks = 0
    failure_count = 0
    for case in cases:
        ranked = _ranked(case)
        anchors = {item.memory_id for item in ranked}
        try:
            outcome = selector.select(
                case.question,
                case.options,
                ranked,
                anchors,
                GraphContext(
                    relation_source_pairs=case.context_pairs,
                    source_context_enabled=case.source_context,
                ),
                request_user_id=_USER,
            )
        except Exception:
            failure_count += 1
            print(json.dumps({"case": case.name, "passed": False,
                              "failure_category": "probe_error"}, sort_keys=True),
                  file=output, flush=True)
            continue
        ok = _passed(case, outcome)
        passed += int(ok)
        positive_passed += int(ok and case.positive)
        negative_passed += int(ok and not case.positive)
        fallbacks += int(outcome.graph.fallback)
        failure_count += int(outcome.graph.failure_category != "none")
        print(json.dumps({
            "case": case.name,
            "positive": case.positive,
            "passed": ok,
            "evidence_state": outcome.selection.evidence_state,
            "abstained": outcome.selection.abstained,
            "fallback": outcome.graph.fallback,
            "failure_category": outcome.graph.failure_category,
            "selected_count": len(outcome.selection.evidence),
            "selected_source_count": outcome.selection.selected_source_count,
            "link_count": outcome.graph.link_count,
        }, sort_keys=True), file=output, flush=True)
    eligible = (
        passed == len(cases)
        and positive_passed == positive_total
        and negative_passed == negative_total
        and fallbacks == 0
        and failure_count == 0
    )
    print(json.dumps({
        "case": "summary",
        "case_count": len(cases),
        "positive_total": positive_total,
        "positive_passed": positive_passed,
        "negative_total": negative_total,
        "negative_passed": negative_passed,
        "fallbacks": fallbacks,
        "failure_count": failure_count,
        "candidate_eligible": eligible,
    }, sort_keys=True), file=output, flush=True)
    return 0 if eligible else 1


def run_stability_probe(llm: StructuredLLM, output: TextIO) -> int:
    """执行预声明的五轮完整门槛；任一轮失败都拒绝候选。"""
    round_count = 5
    rounds_passed = 0
    for round_number in range(1, round_count + 1):
        passed = run_probe(llm, output) == 0
        rounds_passed += int(passed)
        print(
            json.dumps(
                {"case": "round_result", "round": round_number, "passed": passed},
                sort_keys=True,
            ),
            file=output,
            flush=True,
        )
    eligible = rounds_passed == round_count
    print(
        json.dumps(
            {
                "case": "stability_summary",
                "round_count": round_count,
                "rounds_passed": rounds_passed,
                "candidate_eligible": eligible,
            },
            sort_keys=True,
        ),
        file=output,
        flush=True,
    )
    return 0 if eligible else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reverse", action="store_true", help="Reverse the fixed case order")
    mode.add_argument(
        "--stability",
        action="store_true",
        help="Run the fixed suite five complete rounds as one eligibility gate",
    )
    mode.add_argument(
        "--provider-check",
        action="store_true",
        help="Run one planner call and report only safe Provider failure metadata",
    )
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        settings = Settings.from_env()
    except Exception:
        print('{"case":"probe_error","passed":false}')
        return 2
    if settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM:
        print('{"case":"configuration","passed":false}')
        return 2
    try:
        settings.validate_runtime()
        llm = OpenAICompatibleLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_attempts=1,
        )
        try:
            if args.provider_check:
                return run_provider_check(llm, sys.stdout)
            if args.stability:
                return run_stability_probe(llm, sys.stdout)
            return run_probe(llm, sys.stdout, reverse=args.reverse)
        finally:
            llm.close()
    except Exception:
        print('{"case":"probe_error","passed":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
