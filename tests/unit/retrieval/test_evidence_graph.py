"""Grounded evidence graph: cited proposals are validated and assembled deterministically.

Every accepted support must quote text the planner actually saw, every accepted link must be
provable (adjacent same-source context, a persisted relation sidecar entry, or an explicit
discriminative mention), and the three-state verdict is derived by deterministic code rather
than trusted from the model.
"""

import json
from collections.abc import Mapping, Sequence
from uuid import UUID

import pytest

from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_graph import (
    EvidenceGraphPlanner,
    GraphContext,
    GraphEvidenceSelector,
    GraphSelection,
)
from masm.retrieval.evidence_selector import EvidenceSelector, SelectionResult
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ImageURLPart

_USER = "user-1"
_IMAGE_QUERY = [ImageURLPart(image_url={"url": "data:image/png;base64,AAAA"})]


def _evidence(
    number: int,
    source: str,
    content: str,
    *,
    position: int = 0,
    granularity: str = "message",
    conflict_group_id: UUID | None = None,
    source_context: str | None = None,
    user_id: str = _USER,
) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id=user_id,
        content=content,
        score=1.0 / number,
        rank=number,
        request_id=source,
        granularity=granularity,  # type: ignore[arg-type]
        source_position=position,
        conflict_group_id=conflict_group_id,
        source_context=source_context,
        metadata={"lexical": 0.05},
    )


def _proposal(
    *,
    requirements: Sequence[Mapping[str, object]] = (),
    supports: Sequence[Mapping[str, object]] = (),
    links: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return {"requirements": list(requirements), "supports": list(supports), "links": list(links)}


def _requirement(quote: str, kind: str = "attribute") -> dict[str, object]:
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


class _RequestScopedSelector:
    """Keep unit cases concise while exercising the required production user scope."""

    def __init__(self, selector: GraphEvidenceSelector) -> None:
        self._selector = selector

    def select(
        self,
        *args: object,
        request_user_id: str = _USER,
        **kwargs: object,
    ) -> GraphSelection:
        return self._selector.select(  # type: ignore[arg-type]
            *args,
            request_user_id=request_user_id,
            **kwargs,
        )


def _selector(llm: FakeStructuredLLM, *, max_candidates: int = 32) -> _RequestScopedSelector:
    return _RequestScopedSelector(GraphEvidenceSelector(
        EvidenceGraphPlanner(llm, max_candidates=max_candidates),
        EvidenceSelector(llm, max_candidates=max_candidates),
        additive=False,
    ))


def _anchors(*items: RankedEvidence) -> set[UUID]:
    return {item.memory_id for item in items}


def _texts(evidence: Sequence[RankedEvidence]) -> list[str]:
    return [str(item.source_context or item.content) for item in evidence]


# --- 1. direct ---------------------------------------------------------------------------


def test_direct_single_source_fact_is_sufficient() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key?", "location")],
                supports=[_support(0, 0, "I stored the amber key in drawer three.", "direct")],
            )
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    distractor = _evidence(2, "source-b", "Bicycles are stored in the shed.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct, distractor],
        _anchors(direct, distractor),
        GraphContext(),
    )

    assert outcome.selection.evidence == (direct,)
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.abstained is False
    assert outcome.selection.fallback is False
    assert outcome.graph.failure_category == "none"
    assert outcome.graph.planner_calls == 1
    assert outcome.graph.support_count == 1
    assert outcome.graph.candidate_count == 2
    assert outcome.graph.component_count == 1
    assert outcome.graph.selected_count == 1
    assert outcome.graph.selected_source_count == 1
    assert len(llm.requests) == 1
    assert llm.requests[0].prompt_version == "evidence-graph-planner-v2"
    assert llm.requests[0].max_attempts == 1
    assert llm.requests[0].temperature == 0.0


def test_planner_payload_is_bounded_and_carries_no_identifiers() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where is the amber key?", "location")],
                supports=[_support(0, 0, "amber key in drawer three", "direct")],
            )
        ]
    )
    secret = _evidence(1, "request-secret-42", "The amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where is the amber key?", ["drawer three"], [secret], _anchors(secret), GraphContext()
    )

    assert outcome.selection.evidence == (secret,)
    payload = llm.requests[0].payload
    assert set(payload) == {"question", "visual_query_present", "options", "candidates"}
    assert payload["question"] == "Where is the amber key?"
    assert payload["visual_query_present"] is False
    assert payload["options"] == ["drawer three"]
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "request-secret-42" not in rendered
    assert str(secret.memory_id) not in rendered
    assert _USER not in rendered
    assert payload["candidates"][0]["source_group"] == "source-1"
    assert set(payload["candidates"][0]) == {
        "index",
        "source_group",
        "source_position",
        "granularity",
        "text",
        "signals",
    }


# --- 2. two attributes -------------------------------------------------------------------


def test_two_attributes_in_one_source_are_sufficient_through_adjacent_context() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the project code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, "The project code name is Zephyr.", "direct"),
                    _support(1, 1, "The project review date is April 18.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "The project code name is Zephyr.",
                        "The project review date is April 18.",
                        "same_source_context",
                    )
                ],
            )
        ]
    )
    first = _evidence(1, "source-a", "The project code name is Zephyr.", position=0)
    second = _evidence(2, "source-a", "The project review date is April 18.", position=1)

    outcome = _selector(llm).select(
        "What is the project code name and when is the review?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence == (first, second)
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.graph.link_count == 1
    assert outcome.graph.selected_source_count == 1


def test_two_attributes_from_two_sources_without_a_link_stay_partial() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the project code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, "The project code name is Zephyr.", "direct"),
                    _support(1, 1, "The project review date is April 18.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "The project code name is Zephyr.",
                        "The project review date is April 18.",
                        "same_source_context",
                    )
                ],
            )
        ]
    )
    first = _evidence(1, "source-a", "The project code name is Zephyr.")
    second = _evidence(2, "source-b", "The project review date is April 18.")

    outcome = _selector(llm).select(
        "What is the project code name and when is the review?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(source_context_enabled=False),
    )

    # Different sources with no provable link: the same-source link is dropped, so the
    # deterministic assembler must not claim a complete answer.
    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence_state == "partial"
    assert outcome.selection.abstained is False
    assert outcome.graph.fallback is False
    assert outcome.selection.evidence == (first,)


def test_two_sources_connect_only_through_a_discriminative_mention() -> None:
    blocked = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the Zephyr code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, "The Zephyr code was chosen.", "direct"),
                    _support(1, 1, "The review of Zephyr is in April.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "The Zephyr code was chosen.",
                        "The review of Zephyr is in April.",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )
    first = _evidence(1, "source-a", "The Zephyr code was chosen.")
    second = _evidence(2, "source-b", "The review of Zephyr is in April.")

    outcome = _selector(blocked).select(
        "What is the Zephyr code name and when is the review?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(),
    )

    # "Zephyr" and "review" appear in the question, so they may not connect candidates.
    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence_state == "partial"

    allowed = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, "The Zephyr code was chosen.", "direct"),
                    _support(1, 1, "The review of Zephyr is in April.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "The Zephyr code was chosen.",
                        "The review of Zephyr is in April.",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )

    connected = _selector(allowed).select(
        "What is the code name and when is the review?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(),
    )

    assert connected.graph.link_count == 1
    assert connected.selection.evidence_state == "sufficient"
    assert connected.selection.evidence == (first, second)


# --- 2b. connections the deterministic layer proves without the model ---------------------


def test_same_source_adjacent_candidates_connect_without_a_planner_link() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the project code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, "The project code name is Zephyr.", "direct"),
                    _support(1, 1, "The project review date is April 18.", "direct"),
                ],
            )
        ]
    )
    first = _evidence(1, "source-a", "The project code name is Zephyr.", position=0)
    second = _evidence(2, "source-a", "The project review date is April 18.", position=1)

    outcome = _selector(llm).select(
        "What is the project code name and when is the review?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(source_context_enabled=True),
    )

    # 同来源相邻位置本身就是可证明连接：模型没有提出链接也必须连通。
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (first, second)


def test_cross_source_shared_mention_connects_without_a_planner_link() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the workshop", "location")],
                supports=[
                    _support(0, 1, "The Zephyr workshop is held in Lisbon.", "direct"),
                ],
            )
        ]
    )
    attended = _evidence(1, "source-a", "Nora attended the Zephyr workshop.")
    hosted = _evidence(2, "source-b", "The Zephyr workshop is held in Lisbon.")

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [attended, hosted],
        _anchors(attended, hosted),
        GraphContext(),
    )

    # 两段候选正文共享问题中不存在的区分单元（Zephyr），代码可直接建边。
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (attended, hosted)


def test_generic_or_question_bound_units_never_create_cross_source_edges() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the project workshop", "location")],
                supports=[
                    _support(0, 1, "The workshop is in Lisbon.", "direct"),
                    _support(0, 0, "The project workshop was planned.", "context"),
                ],
            )
        ]
    )
    planned = _evidence(1, "source-a", "The project workshop was planned.")
    hosted = _evidence(2, "source-b", "The workshop is in Lisbon.")

    outcome = _selector(llm).select(
        "Which city hosts the project workshop?",
        None,
        [planned, hosted],
        _anchors(planned, hosted),
        GraphContext(),
    )

    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence == (hosted,)


def test_source_context_json_boilerplate_never_creates_edges() -> None:
    def _context(text: str) -> str:
        return json.dumps(
            {
                "matched_position": 0,
                "messages": [
                    {"position": 0, "role": "user", "text": text, "timestamp": None}
                ],
                "omitted_positions": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the check-in code", "attribute"),
                    _requirement("where is the venue", "location"),
                ],
                supports=[
                    _support(0, 0, _context("The check-in code is K7-42."), "direct"),
                    _support(1, 1, _context("The venue is the old cinema."), "direct"),
                ],
            )
        ]
    )
    code = _evidence(
        1,
        "source-a",
        "The check-in code is K7-42.",
        source_context=_context("The check-in code is K7-42."),
    )
    venue = _evidence(
        2,
        "source-b",
        "The venue is the old cinema.",
        source_context=_context("The venue is the old cinema."),
    )

    outcome = _selector(llm).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(source_context_enabled=True),
    )

    # 来源上下文是 JSON：结构键与 JSON 字面量不是区分单元，不得把两个来源连起来。
    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence_state == "partial"
    assert len(outcome.selection.evidence) == 1


# --- 2c. completeness: unclaimed or ungrounded question content cannot be sufficient ----


def _chain_candidates() -> tuple[RankedEvidence, RankedEvidence]:
    return (
        _evidence(1, "source-a", "Nora attended the Zephyr workshop."),
        _evidence(2, "source-b", "The Zephyr workshop is held in Lisbon."),
    )


def test_unclaimed_question_content_blocks_sufficiency() -> None:
    planned = _evidence(1, "source-a", "Nora planned a workshop.")
    hosted = _evidence(2, "source-b", "The workshop is in Lisbon.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the workshop", "location")],
                supports=[_support(0, 1, "The workshop is in Lisbon.", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [planned, hosted],
        _anchors(planned, hosted),
        GraphContext(),
    )

    # 问题里的“that Nora attended”没有被任何需求覆盖，两条候选也没有可证明连接可以补上它。
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.evidence == ()


def test_chain_stays_sufficient_when_the_binding_evidence_is_also_cited() -> None:
    attended, hosted = _chain_candidates()
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement(
                        "Which city hosts the workshop that Nora attended", "location"
                    )
                ],
                supports=[
                    _support(0, 0, "Nora attended the Zephyr workshop.", "context"),
                    _support(0, 1, "The Zephyr workshop is held in Lisbon.", "direct"),
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [attended, hosted],
        _anchors(attended, hosted),
        GraphContext(),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (attended, hosted)


def test_chinese_unattested_units_do_not_block_a_grounded_completion() -> None:
    enrolled = _evidence(1, "source-a", "我报名了陶艺工作坊。")
    hosted = _evidence(2, "source-b", "陶艺工作坊在里斯本举办。")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("在哪个城市举办", "location"),
                    _requirement("我报名的课程", "subject"),
                ],
                supports=[
                    _support(1, 0, "我报名了陶艺工作坊。", "direct"),
                    _support(0, 1, "陶艺工作坊在里斯本举办。", "direct"),
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "我报名的课程在哪个城市举办？",
        None,
        [enrolled, hosted],
        _anchors(enrolled, hosted),
        GraphContext(),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (enrolled, hosted)
    # 陶艺 在两个候选里各出现一次：代码据此建边，两个候选合为一个连通分量。
    assert outcome.graph.component_count == 1


def test_chinese_negative_without_grounded_binding_content_abstains() -> None:
    generic = _evidence(1, "source-a", "我报名了一个工作坊。")
    hosted = _evidence(2, "source-b", "这个工作坊在里斯本举办。")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("工作坊在哪个城市举办", "location")],
                supports=[_support(0, 1, "这个工作坊在里斯本举办。", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "我报名的课程，工作坊在哪个城市举办？",
        None,
        [generic, hosted],
        _anchors(generic, hosted),
        GraphContext(),
    )

    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.evidence == ()


def test_multi_clause_question_cannot_be_sufficient_without_the_binding_evidence() -> None:
    planned = _evidence(1, "source-a", "Nora planned a workshop.")
    hosted = _evidence(2, "source-b", "The workshop is in Lisbon.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement(
                        "Which city hosts the workshop that Nora attended", "location"
                    )
                ],
                supports=[_support(0, 1, "The workshop is in Lisbon.", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [planned, hosted],
        _anchors(planned, hosted),
        GraphContext(),
    )

    # 关系从句 that Nora attended 被吞进唯一需求引文，但没有任何已选证据覆盖它。
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.evidence == ()


def test_chinese_comma_question_cannot_be_sufficient_without_the_binding_content() -> None:
    generic = _evidence(1, "source-a", "我报名了一个工作坊。")
    hosted = _evidence(2, "source-b", "这个工作坊在里斯本举办。")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("我报名的课程，工作坊在哪个城市举办", "location")],
                supports=[_support(0, 1, "这个工作坊在里斯本举办。", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "我报名的课程，工作坊在哪个城市举办？",
        None,
        [generic, hosted],
        _anchors(generic, hosted),
        GraphContext(),
    )

    # “我报名的课程”被吞进唯一需求引文，已选证据只覆盖“举办”，不得判为完整。
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.evidence == ()


def test_binding_preposition_question_cannot_be_sufficient_without_its_value() -> None:
    stored = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the storage code for the amber key", "attribute")
                ],
                supports=[
                    _support(0, 0, "I stored the amber key in drawer three.", "direct")
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "What is the storage code for the amber key?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
    )

    # 问题问的是 storage code，候选只说明放在哪个抽屉：绑定内容的单元未被覆盖，不得判为完整。
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.evidence == ()


def test_elliptical_answer_without_a_clause_marker_stays_sufficient() -> None:
    asked = _evidence(1, "source-a", "Did you buy the blue bicycle?")
    answered = _evidence(2, "source-b", "No, I bought the red one.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 1, "No, I bought the red one.", "direct"),
                    _support(0, 0, "Did you buy the blue bicycle?", "context"),
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [asked, answered],
        _anchors(asked, answered),
        GraphContext(),
    )

    # 单子句问题的省略回答（red one）合法：没有子句标记就不启用完整性收紧。
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (answered,)


def test_persisted_relation_pair_connects_without_a_planner_link() -> None:
    code = _evidence(1, "source-a", "The check-in code is K7-42.")
    venue = _evidence(2, "source-b", "The venue is the old cinema.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the check-in code", "attribute"),
                    _requirement("where is the venue", "location"),
                ],
                supports=[
                    _support(0, 0, "The check-in code is K7-42.", "direct"),
                    _support(1, 1, "The venue is the old cinema.", "direct"),
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
    )

    # 侧车已存关系本身就是可证明连接：模型没有提出 persisted_relation 也必须连通。
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (code, venue)


def test_persisted_relation_edges_require_the_sidecar_pair() -> None:
    code = _evidence(1, "source-a", "The check-in code is K7-42.")
    venue = _evidence(2, "source-b", "The venue is the old cinema.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the check-in code", "attribute"),
                    _requirement("where is the venue", "location"),
                ],
                supports=[
                    _support(0, 0, "The check-in code is K7-42.", "direct"),
                    _support(1, 1, "The venue is the old cinema.", "direct"),
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(),
    )

    # 没有侧车对：跨来源候选绝不因此连通，只能部分返回。
    assert outcome.selection.evidence_state == "partial"
    assert len(outcome.selection.evidence) == 1


def test_uncited_requirement_counts_as_covered_when_its_units_are_grounded() -> None:
    enrolled = _evidence(1, "source-a", "我报名了陶艺工作坊。")
    hosted = _evidence(2, "source-b", "陶艺工作坊在里斯本举办。")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("在哪个城市举办", "location"),
                    _requirement("我报名的课程", "subject"),
                ],
                supports=[_support(0, 1, "陶艺工作坊在里斯本举办。", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "我报名的课程在哪个城市举办？",
        None,
        [enrolled, hosted],
        _anchors(enrolled, hosted),
        GraphContext(),
    )

    # 第二个需求没有模型声明的支持，但它与已覆盖需求同一分量，且其区分单元被已选证据覆盖。
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (enrolled, hosted)


def test_uncited_requirement_is_not_covered_without_grounded_units() -> None:
    generic = _evidence(1, "source-a", "我报名了一个工作坊。")
    hosted = _evidence(2, "source-b", "这个工作坊在里斯本举办。")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("工作坊在哪个城市举办", "location"),
                    _requirement("我报名的课程", "subject"),
                ],
                supports=[_support(0, 1, "这个工作坊在里斯本举办。", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "我报名的课程，工作坊在哪个城市举办？",
        None,
        [generic, hosted],
        _anchors(generic, hosted),
        GraphContext(),
    )

    # 两条候选之间没有任何可证明连接：未被引用的“我报名的课程”不得由别处的证据确立。
    assert outcome.selection.evidence_state != "sufficient"
    assert outcome.selection.evidence == (hosted,)


def test_unit_free_requirement_is_never_auto_covered() -> None:
    code = _evidence(1, "source-a", "The check-in code is K7-42.")
    venue = _evidence(2, "source-b", "The venue is the old cinema.")
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the check-in code", "attribute"),
                    _requirement("where is the venue", "location"),
                ],
                supports=[_support(0, 0, "The check-in code is K7-42.", "direct")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
    )

    # 侧车对把两条证据连成同一分量，但“where is the venue”没有区分单元可供核对：
    # 代码不得替模型确立这条需求，只能部分返回。
    assert outcome.selection.evidence_state == "partial"
    assert outcome.selection.evidence == (code,)


# --- additive mode: the frozen v7 selection is the baseline ------------------------------


class _StubBaseline:
    """冻结 v7 选择的替身：返回固定证据并记录调用次数。"""

    def __init__(self, evidence: tuple[RankedEvidence, ...]) -> None:
        self._evidence = evidence
        self.calls = 0

    def select(
        self,
        query: object,
        options: object,
        ranked: object,
        strong_anchor_ids: object,
    ) -> SelectionResult:
        self.calls += 1
        return SelectionResult(
            evidence=self._evidence,
            candidate_count=len(self._evidence),
            source_count=len({item.request_id for item in self._evidence}),
            selected_source_count=len({item.request_id for item in self._evidence}),
            fallback=False,
            abstained=not self._evidence,
            failure_category="none",
            evidence_state="sufficient" if self._evidence else "insufficient",
        )


def _additive_selector(
    llm: FakeStructuredLLM, baseline: _StubBaseline
) -> GraphEvidenceSelector:
    return GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        baseline,  # type: ignore[arg-type]
        additive=True,
    )


def test_additive_mode_keeps_the_baseline_and_appends_proven_evidence() -> None:
    baseline_item = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    added_item = _evidence(2, "source-b", "The drawer three key is the amber one.")
    baseline = _StubBaseline((baseline_item,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[
                    _support(0, 1, "The drawer three key is the amber one.", "direct")
                ],
            )
        ]
    )

    outcome = _additive_selector(llm, baseline).select(
        "Where did I store the amber key?",
        None,
        [baseline_item, added_item],
        _anchors(baseline_item, added_item),
        GraphContext(),
        request_user_id="user-1",
    )

    # 基线原样保留、追加在后，且只调用一次基线选择。
    assert outcome.selection.evidence == (baseline_item, added_item)
    assert baseline.calls == 1


def test_additive_mode_keeps_the_baseline_when_the_planner_fails() -> None:
    baseline_item = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    baseline = _StubBaseline((baseline_item,))
    llm = FakeStructuredLLM([])

    outcome = _additive_selector(llm, baseline).select(
        "Where did I store the amber key?",
        None,
        [baseline_item],
        _anchors(baseline_item),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.selection.evidence == (baseline_item,)
    assert outcome.graph.fallback is True
    assert baseline.calls == 1


def test_additive_mode_never_appends_partial_graph_evidence() -> None:
    baseline_item = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    other_item = _evidence(2, "source-b", "The drawer three key is the amber one.")
    baseline = _StubBaseline((baseline_item,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("Where did I store the amber key", "location"),
                    _requirement("when did I last use it", "time"),
                ],
                supports=[
                    _support(0, 1, "The drawer three key is the amber one.", "direct")
                ],
                links=[],
            )
        ]
    )
    selector = _additive_selector(llm, baseline)

    outcome = selector.select(
        "Where did I store the amber key and when did I last use it?",
        None,
        [baseline_item, other_item],
        _anchors(baseline_item, other_item),
        GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
        request_user_id="user-1",
    )

    # 第二个需求没有证据，判定为部分覆盖：部分证据不得追加到基线之后。
    assert outcome.graph.state == "partial"
    assert outcome.selection.evidence == (baseline_item,)


# --- R10: refuse when the named subject is absent from the whole candidate pool ----------


def test_additive_mode_refuses_when_the_named_subject_is_absent() -> None:
    stored = _evidence(1, "source-a", "The access code for the item-42 locker is code-7.")
    baseline = _StubBaseline((stored,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What is the access code", "location")],
                supports=[_support(0, 0, "The access code for the item-42 locker", "direct")],
            )
        ]
    )

    selector = GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        baseline,  # type: ignore[arg-type]
        additive=True,
        presence=lambda user_id, tokens: frozenset(),
        # 库里存在"同框架、不同主体"的记忆（近失配）才允许拒答。
        frames=lambda user_id, question: ("The access code for the item-42 locker is code-7.",),
    )
    outcome = selector.select(
        "What is the access code for the lockB-77 locker?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.graph.vetoed is True
    assert outcome.selection.evidence == ()
    assert outcome.selection.abstained is True


def test_additive_mode_keeps_evidence_for_a_present_named_subject() -> None:
    stored = _evidence(1, "source-a", "The access code for the item-42 locker is code-7.")
    baseline = _StubBaseline((stored,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What is the access code", "location")],
                supports=[_support(0, 0, "The access code for the item-42 locker", "direct")],
            )
        ]
    )

    outcome = _additive_selector(llm, baseline).select(
        "What is the access code for the item-42 locker?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.graph.vetoed is False
    assert outcome.selection.evidence == (stored,)

# --- 3. explicit cross-source chain ------------------------------------------------------


def test_explicit_cross_source_chain_is_sufficient() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("Which city hosts the workshop", "location"),
                    _requirement("that Nora attended", "subject"),
                ],
                supports=[
                    _support(1, 0, "Nora attended the Zephyr workshop.", "direct"),
                    _support(0, 1, "The Zephyr workshop is held in Lisbon.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "Nora attended the Zephyr workshop.",
                        "The Zephyr workshop is held in Lisbon.",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )
    attended = _evidence(1, "source-a", "Nora attended the Zephyr workshop.")
    hosted = _evidence(2, "source-b", "The Zephyr workshop is held in Lisbon.")

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [attended, hosted],
        _anchors(attended, hosted),
        GraphContext(),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (attended, hosted)
    assert outcome.graph.link_count == 1
    assert outcome.graph.selected_source_count == 2
    assert _texts(outcome.selection.evidence) == [
        "Nora attended the Zephyr workshop.",
        "The Zephyr workshop is held in Lisbon.",
    ]


# --- 4. elliptical confirmation ----------------------------------------------------------


def test_elliptical_confirmation_returns_the_adjacent_question_turn() -> None:
    proposal = _proposal(
        requirements=[_requirement("Which city did I move to", "location")],
        supports=[
            _support(0, 0, "Did you move to Porto?", "context"),
            _support(0, 1, "Yes, last week.", "direct"),
        ],
        links=[_link(0, 1, "Did you move to Porto?", "Yes, last week.", "confirmation")],
    )
    asked = _evidence(1, "source-a", "Did you move to Porto?", position=0)
    confirmed = _evidence(2, "source-a", "Yes, last week.", position=1)

    outcome = _selector(FakeStructuredLLM([proposal])).select(
        "Which city did I move to?",
        None,
        [asked, confirmed],
        _anchors(asked, confirmed),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (asked, confirmed)
    assert outcome.graph.link_count == 1

    without_context = _selector(FakeStructuredLLM([proposal])).select(
        "Which city did I move to?",
        None,
        [asked, confirmed],
        _anchors(asked, confirmed),
        GraphContext(source_context_enabled=False),
    )

    # Without source context the adjacency is unavailable, so only the directly grounded
    # answer turn is returned instead of an unproven pair.
    assert without_context.graph.link_count == 0
    assert without_context.selection.evidence == (confirmed,)
    assert without_context.selection.evidence_state == "sufficient"


def test_confirmation_link_keeps_context_even_when_planner_omits_context_support() -> None:
    proposal = _proposal(
        requirements=[_requirement("Which city did I move to", "location")],
        supports=[_support(0, 1, "Yes, last week.", "direct")],
        links=[_link(0, 1, "Did you move to Porto?", "Yes, last week.", "confirmation")],
    )
    asked = _evidence(1, "source-a", "Did you move to Porto?", position=0)
    confirmed = _evidence(2, "source-a", "Yes, last week.", position=1)

    outcome = _selector(FakeStructuredLLM([proposal])).select(
        "Which city did I move to?",
        None,
        [asked, confirmed],
        _anchors(asked, confirmed),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (asked, confirmed)
    assert outcome.graph.link_count == 1


# --- 5. forward correction ---------------------------------------------------------------


def test_forward_correction_keeps_both_turns() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 0, "Did you buy the blue bicycle?", "context"),
                    _support(0, 1, "No, I bought the red one.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "Did you buy the blue bicycle?",
                        "No, I bought the red one.",
                        "correction",
                    )
                ],
            )
        ]
    )
    asked = _evidence(1, "source-a", "Did you buy the blue bicycle?", position=0)
    corrected = _evidence(2, "source-a", "No, I bought the red one.", position=1)

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [asked, corrected],
        _anchors(asked, corrected),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (asked, corrected)
    joined = " ".join(_texts(outcome.selection.evidence))
    assert "red" in joined
    assert "blue" in joined


def test_correction_link_keeps_corrected_context_when_planner_omits_context_support() -> None:
    proposal = _proposal(
        requirements=[_requirement("What color bicycle did I buy", "attribute")],
        supports=[_support(0, 1, "No, I bought the red one.", "direct")],
        links=[
            _link(
                0,
                1,
                "Did you buy the blue bicycle?",
                "No, I bought the red one.",
                "correction",
            )
        ],
    )
    asked = _evidence(1, "source-a", "Did you buy the blue bicycle?", position=0)
    corrected = _evidence(2, "source-a", "No, I bought the red one.", position=1)

    outcome = _selector(FakeStructuredLLM([proposal])).select(
        "What color bicycle did I buy?",
        None,
        [asked, corrected],
        _anchors(asked, corrected),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (asked, corrected)
    assert outcome.graph.link_count == 1


def test_unproven_correction_link_is_rejected() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 0, "Did you buy the blue bicycle?", "context"),
                    _support(0, 1, "No, I bought the red one.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "Did you buy the blue bicycle?",
                        "No, I bought the red one.",
                        "correction",
                    )
                ],
            )
        ]
    )
    asked = _evidence(1, "source-a", "Did you buy the blue bicycle?", position=0)
    corrected = _evidence(2, "source-b", "No, I bought the red one.", position=0)

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [asked, corrected],
        _anchors(asked, corrected),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence == (corrected,)
    assert outcome.selection.evidence_state == "sufficient"


# --- 6. persisted two-hop provenance -----------------------------------------------------


def test_persisted_relation_requires_the_provenance_sidecar() -> None:
    proposal = _proposal(
        requirements=[
            _requirement("What is the check-in code", "attribute"),
            _requirement("where is the venue", "location"),
        ],
        supports=[
            _support(0, 0, "The check-in code is K7-42.", "direct"),
            _support(1, 1, "The venue is the old cinema.", "direct"),
        ],
        links=[
            _link(
                0,
                1,
                "The check-in code is K7-42.",
                "The venue is the old cinema.",
                "persisted_relation",
            )
        ],
    )
    code = _evidence(1, "source-a", "The check-in code is K7-42.")
    venue = _evidence(2, "source-b", "The venue is the old cinema.")

    without_sidecar = _selector(FakeStructuredLLM([proposal])).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(),
    )
    assert without_sidecar.graph.link_count == 0
    assert without_sidecar.selection.evidence_state == "partial"

    with_sidecar = _selector(FakeStructuredLLM([proposal])).select(
        "What is the check-in code and where is the venue?",
        None,
        [code, venue],
        _anchors(code, venue),
        GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
    )
    assert with_sidecar.graph.link_count == 1
    assert with_sidecar.selection.evidence_state == "sufficient"
    assert with_sidecar.selection.evidence == (code, venue)


# --- 7-10. refusal cases -----------------------------------------------------------------


def test_wrong_subject_bridge_only_graph_abstains() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city did I move to", "location")],
                supports=[_support(0, 0, "My colleague moved to Porto.", "bridge")],
            )
        ]
    )
    wrong_subject = _evidence(
        1, "source-a", "My colleague moved to Porto. I have not shared my own destination."
    )

    outcome = _selector(llm).select(
        "Which city did I move to?",
        None,
        [wrong_subject],
        _anchors(wrong_subject),
        GraphContext(),
    )

    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.abstained is True
    assert outcome.graph.fallback is False
    assert len(llm.requests) == 1


def test_generic_shared_noun_cannot_connect_disconnected_candidates() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the project workshop", "location")],
                supports=[
                    _support(0, 0, "The project workshop was planned.", "context"),
                    _support(0, 1, "The workshop takes place every spring.", "context"),
                    _support(0, 2, "The project workshop is in Lisbon.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        2,
                        "The project workshop was planned.",
                        "The project workshop is in Lisbon.",
                        "shared_explicit_mention",
                    ),
                    _link(
                        1,
                        2,
                        "The workshop takes place every spring.",
                        "The project workshop is in Lisbon.",
                        "shared_explicit_mention",
                    ),
                ],
            )
        ]
    )
    planned = _evidence(1, "source-a", "The project workshop was planned.")
    spring = _evidence(2, "source-b", "The workshop takes place every spring.")
    hosted = _evidence(3, "source-c", "The project workshop is in Lisbon.")

    outcome = _selector(llm).select(
        "Which city hosts the project workshop?",
        None,
        [planned, spring, hosted],
        _anchors(planned, spring, hosted),
        GraphContext(),
    )

    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence == (hosted,)
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.selected_source_count == 1


def test_missing_requested_value_abstains() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What is the storage code", "attribute")],
                supports=[_support(0, 0, "the amber key", "context")],
            )
        ]
    )
    nearby = _evidence(1, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "What is the storage code for the amber key?",
        None,
        [nearby],
        _anchors(nearby),
        GraphContext(),
    )

    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.graph.fallback is False


def test_multiple_choice_options_cannot_create_support() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[_support(0, 0, "drawer three", "direct")],
            )
        ]
    )
    stored = _evidence(1, "source-a", "The amber key matters to me.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        ["drawer three", "shelf two"],
        [stored],
        _anchors(stored),
        GraphContext(),
    )

    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    # The option text was never quoted from the candidate, so nothing was validated.
    assert outcome.graph.support_count == 0
    assert llm.requests[0].payload["options"] == ["drawer three", "shelf two"]
    assert outcome.graph.fallback is False


# --- 11-12. distractors and crowding -----------------------------------------------------


def test_high_ranked_strong_anchor_distractor_is_excluded() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[_support(0, 1, "I stored the amber key in drawer three.", "direct")],
            )
        ]
    )
    distractor = _evidence(1, "source-z", "Drawer three is a common storage place.")
    direct = _evidence(2, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [distractor, direct],
        _anchors(distractor, direct),
        GraphContext(),
    )

    assert outcome.selection.evidence == (direct,)
    assert outcome.selection.evidence_state == "sufficient"


def test_strong_anchors_alone_never_produce_evidence() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[],
            )
        ]
    )
    anchor = _evidence(1, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [anchor],
        _anchors(anchor),
        GraphContext(),
    )

    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.graph.fallback is False


def test_crowded_32_candidate_positive_case_returns_only_the_chain() -> None:
    candidates = [
        _evidence(index + 1, f"source-{index:02d}", f"Unrelated note number {index}.")
        for index in range(32)
    ]
    bridge = _evidence(101, "source-30", "Nora attended the Zephyr workshop.")
    hosted = _evidence(102, "source-31", "The Zephyr workshop is held in Lisbon.")
    ranked = [*candidates[:20], bridge, *candidates[20:30], hosted, *candidates[30:]]
    assert len(ranked) == 34
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("Which city hosts the workshop", "location"),
                    _requirement("that Nora attended", "subject"),
                ],
                supports=[
                    _support(1, 20, "Nora attended the Zephyr workshop.", "direct"),
                    _support(0, 31, "The Zephyr workshop is held in Lisbon.", "direct"),
                ],
                links=[
                    _link(
                        20,
                        31,
                        "Nora attended the Zephyr workshop.",
                        "The Zephyr workshop is held in Lisbon.",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        ranked,
        _anchors(*ranked),
        GraphContext(),
    )

    assert outcome.graph.candidate_count == 32
    assert len(llm.requests[0].payload["candidates"]) == 32
    assert outcome.selection.evidence == (bridge, hosted)
    assert outcome.selection.evidence_state == "sufficient"


def test_crowded_32_candidate_negative_case_abstains() -> None:
    ranked = [
        _evidence(index + 1, f"source-{index:02d}", f"Unrelated note number {index}.")
        for index in range(32)
    ]
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the workshop", "location")],
                supports=[_support(0, 0, "Unrelated note number 0.", "context")],
            )
        ]
    )

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        ranked,
        _anchors(*ranked),
        GraphContext(),
    )

    assert outcome.graph.candidate_count == 32
    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.graph.fallback is False


# --- 13-14. conflicts and duplicates -----------------------------------------------------


def test_conflicting_support_in_one_stored_conflict_group_keeps_both_sides() -> None:
    group = UUID(int=999)
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 0, "I bought the blue bicycle.", "direct"),
                    _support(0, 1, "I did not buy a blue bicycle.", "negative"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "I bought the blue bicycle.",
                        "I did not buy a blue bicycle.",
                        "same_source_context",
                    )
                ],
            )
        ]
    )
    blue = _evidence(
        1, "source-a", "I bought the blue bicycle.", position=0, conflict_group_id=group
    )
    denied = _evidence(
        2, "source-a", "I did not buy a blue bicycle.", position=1, conflict_group_id=group
    )

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [blue, denied],
        _anchors(blue, denied),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (blue, denied)


def test_stored_conflict_group_connects_both_sides_without_planner_link() -> None:
    group = UUID(int=1001)
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 0, "I bought the blue bicycle.", "direct"),
                    _support(0, 1, "I did not buy a blue bicycle.", "negative"),
                ],
                links=[],
            )
        ]
    )
    blue = _evidence(1, "source-a", "I bought the blue bicycle.", conflict_group_id=group)
    denied = _evidence(
        2,
        "source-b",
        "I did not buy a blue bicycle.",
        conflict_group_id=group,
    )

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [blue, denied],
        _anchors(blue, denied),
        GraphContext(),
    )

    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (blue, denied)


def test_conflicting_support_outside_a_conflict_group_is_partial() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What color bicycle did I buy", "attribute")],
                supports=[
                    _support(0, 0, "I bought the blue bicycle.", "direct"),
                    _support(0, 1, "I did not buy a blue bicycle.", "negative"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "I bought the blue bicycle.",
                        "I did not buy a blue bicycle.",
                        "same_source_context",
                    )
                ],
            )
        ]
    )
    blue = _evidence(1, "source-a", "I bought the blue bicycle.", position=0)
    denied = _evidence(2, "source-a", "I did not buy a blue bicycle.", position=1)

    outcome = _selector(llm).select(
        "What color bicycle did I buy?",
        None,
        [blue, denied],
        _anchors(blue, denied),
        GraphContext(source_context_enabled=True),
    )

    # Both sides stay visible, but an unresolved contradiction is never reported complete.
    assert outcome.selection.evidence_state == "partial"
    assert outcome.selection.evidence == (blue, denied)


def test_duplicate_representation_of_one_source_counts_once() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[
                    _support(0, 0, "I stored the amber key in drawer three.", "direct"),
                    _support(0, 1, "I stored the amber key in drawer three.", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "I stored the amber key in drawer three.",
                        "I stored the amber key in drawer three.",
                        "same_source_context",
                    )
                ],
            )
        ]
    )
    context_copy = _evidence(
        1,
        "source-a",
        "I stored the amber key in drawer three.",
        position=0,
        granularity="context",
    )
    message_copy = _evidence(
        2,
        "source-a",
        "I stored the amber key in drawer three.",
        position=0,
        granularity="message",
    )

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [context_copy, message_copy],
        _anchors(context_copy, message_copy),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence == (context_copy,)
    assert outcome.graph.selected_source_count == 1
    assert outcome.graph.selected_count == 1


def test_unrelated_candidates_are_never_added_to_fill_the_budget() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[
                    _support(0, 0, "I stored the amber key in drawer three.", "direct"),
                    _support(0, 1, "The shed also holds tools.", "context"),
                ],
            )
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    linked_context = _evidence(2, "source-a", "The shed also holds tools.", position=1)
    unrelated = _evidence(3, "source-c", "Unrelated note about bicycles.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct, linked_context, unrelated],
        _anchors(direct, linked_context, unrelated),
        GraphContext(source_context_enabled=False),
    )

    # The declared context turn is not connected by any validated link and the unrelated
    # candidate was never cited: neither may pad the response.
    assert outcome.selection.evidence == (direct,)
    assert outcome.selection.evidence_state == "sufficient"


def test_over_budget_partial_remains_connected() -> None:
    question = "What is the code name and when is the review?"
    notes = [f"Note {index} about the Zephyr review." for index in range(14)]
    nodes = [
        _evidence(index + 1, "source-a", notes[index], position=index) for index in range(14)
    ]
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("What is the code name", "attribute"),
                    _requirement("when is the review", "time"),
                ],
                supports=[
                    _support(0, 0, notes[0], "direct"),
                    _support(1, 13, notes[13], "direct"),
                ],
                links=[
                    _link(index, index + 1, notes[index], notes[index + 1], "same_source_context")
                    for index in range(13)
                ],
            )
        ]
    )

    outcome = _selector(llm).select(
        question,
        None,
        nodes,
        _anchors(*nodes),
        GraphContext(source_context_enabled=True),
    )

    # 14 connected items exceed the evidence cap: the verdict must degrade instead of
    # silently truncating a claimed complete path.
    assert outcome.graph.link_count == 13
    assert outcome.selection.evidence_state == "partial"
    assert outcome.selection.evidence
    assert len(outcome.selection.evidence) <= 12
    selected_indices = {nodes.index(item) for item in outcome.selection.evidence}
    reached = {min(selected_indices)}
    frontier = list(reached)
    while frontier:
        current = frontier.pop()
        for neighbour in (current - 1, current + 1):
            if neighbour in selected_indices and neighbour not in reached:
                reached.add(neighbour)
                frontier.append(neighbour)
    assert reached == selected_indices


def test_connected_solution_after_512_combinations_is_sufficient() -> None:
    question = "Where is alpha, where is beta, where is gamma, and where is delta?"
    requirement_quotes = ("Where is alpha", "where is beta", "where is gamma", "where is delta")
    candidates: list[RankedEvidence] = []
    supports: list[dict[str, object]] = []
    connected_indices: list[int] = []
    for requirement_index in range(4):
        for choice_index in range(5):
            candidate_index = len(candidates)
            text = f"Grounded support {requirement_index}-{choice_index}."
            is_connected_choice = choice_index == 4
            candidates.append(
                _evidence(
                    candidate_index + 1,
                    "connected-source" if is_connected_choice else f"isolated-{candidate_index}",
                    text,
                    position=requirement_index if is_connected_choice else 0,
                )
            )
            supports.append(_support(requirement_index, candidate_index, text, "direct"))
            if is_connected_choice:
                connected_indices.append(candidate_index)
    links = [
        _link(
            left,
            right,
            str(candidates[left].content),
            str(candidates[right].content),
            "same_source_context",
        )
        for left, right in zip(connected_indices, connected_indices[1:], strict=False)
    ]
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement(quote) for quote in requirement_quotes],
                supports=supports,
                links=links,
            )
        ]
    )

    outcome = _selector(llm).select(
        question,
        None,
        candidates,
        _anchors(*candidates),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == tuple(candidates[index] for index in connected_indices)


# --- 15. Chinese -------------------------------------------------------------------------


def test_chinese_direct_fact_is_sufficient() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("我把琥珀钥匙放在哪里了", "location")],
                supports=[_support(0, 0, "我把琥珀钥匙放在第三个抽屉里。", "direct")],
            )
        ]
    )
    stored = _evidence(1, "source-a", "我把琥珀钥匙放在第三个抽屉里。")

    outcome = _selector(llm).select(
        "我把琥珀钥匙放在哪里了？", None, [stored], _anchors(stored), GraphContext()
    )

    assert outcome.selection.evidence == (stored,)
    assert outcome.selection.evidence_state == "sufficient"


def test_chinese_contextual_confirmation_is_sufficient() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("我搬到哪个城市了", "location")],
                supports=[
                    _support(0, 0, "你搬到波尔图了吗？", "context"),
                    _support(0, 1, "对，就是那里。", "direct"),
                ],
                links=[_link(0, 1, "你搬到波尔图了吗？", "对，就是那里。", "confirmation")],
            )
        ]
    )
    asked = _evidence(1, "source-a", "你搬到波尔图了吗？", position=0)
    confirmed = _evidence(2, "source-a", "对，就是那里。", position=1)

    outcome = _selector(llm).select(
        "我搬到哪个城市了？",
        None,
        [asked, confirmed],
        _anchors(asked, confirmed),
        GraphContext(source_context_enabled=True),
    )

    assert outcome.selection.evidence_state == "sufficient"
    assert outcome.selection.evidence == (asked, confirmed)
    assert outcome.graph.link_count == 1


def test_chinese_generic_workshop_noun_does_not_connect() -> None:
    generic = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("工作坊在哪个城市举办", "location"),
                    _requirement("我报名的课程", "subject"),
                ],
                supports=[
                    _support(1, 0, "我报名了一个工作坊。", "direct"),
                    _support(0, 1, "这个工作坊在里斯本举办。", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "我报名了一个工作坊。",
                        "这个工作坊在里斯本举办。",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )
    enrolled = _evidence(1, "source-a", "我报名了一个工作坊。")
    hosted = _evidence(2, "source-b", "这个工作坊在里斯本举办。")

    outcome = _selector(generic).select(
        "我报名的课程，工作坊在哪个城市举办？",
        None,
        [enrolled, hosted],
        _anchors(enrolled, hosted),
        GraphContext(),
    )

    assert outcome.graph.link_count == 0
    assert outcome.selection.evidence_state == "partial"

    shared = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("在哪个城市举办", "location"),
                    _requirement("我报名的课程", "subject"),
                ],
                supports=[
                    _support(1, 0, "我报名了陶艺工作坊。", "direct"),
                    _support(0, 1, "陶艺工作坊在里斯本举办。", "direct"),
                ],
                links=[
                    _link(
                        0,
                        1,
                        "我报名了陶艺工作坊。",
                        "陶艺工作坊在里斯本举办。",
                        "shared_explicit_mention",
                    )
                ],
            )
        ]
    )
    pottery = _evidence(1, "source-a", "我报名了陶艺工作坊。")
    pottery_hosted = _evidence(2, "source-b", "陶艺工作坊在里斯本举办。")

    connected = _selector(shared).select(
        "我报名的课程在哪个城市举办？",
        None,
        [pottery, pottery_hosted],
        _anchors(pottery, pottery_hosted),
        GraphContext(),
    )

    assert connected.graph.link_count == 1
    assert connected.selection.evidence_state == "sufficient"
    assert connected.selection.evidence == (pottery, pottery_hosted)


# --- 17-19. fallback, abstention and pure image ------------------------------------------


def test_planner_transport_failure_falls_back_to_the_frozen_selector_once() -> None:
    llm = FakeStructuredLLM(
        [
            TimeoutError("private-provider-detail"),
            {"evidence_state": "sufficient", "selected_indices": [0]},
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct],
        _anchors(direct),
        GraphContext(),
    )

    assert outcome.graph.fallback is True
    assert outcome.graph.failure_category == "planner_unavailable"
    assert outcome.selection.evidence == (direct,)
    assert [request.prompt_version for request in llm.requests] == [
        "evidence-graph-planner-v2",
        "evidence-selector-v7",
    ]


def test_planner_failure_falls_back_once_over_the_same_bounded_pool() -> None:
    llm = FakeStructuredLLM(
        [
            TimeoutError("private-provider-detail"),
            {"evidence_state": "insufficient", "selected_indices": []},
            {"evidence_state": "sufficient", "selected_indices": [0]},
        ]
    )
    ranked = [
        _evidence(index, f"source-{index}", f"Candidate {index}.")
        for index in range(1, 41)
    ]

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        ranked,
        _anchors(*ranked),
        GraphContext(),
    )

    assert outcome.graph.fallback is True
    assert outcome.graph.failure_category == "planner_unavailable"
    assert outcome.selection.candidate_count == 32
    assert [request.prompt_version for request in llm.requests] == [
        "evidence-graph-planner-v2",
        "evidence-selector-v7",
    ]


def _invalid_proposals() -> list[dict[str, object]]:
    grounded = {"question_quote": "Where did I store the amber key", "kind": "location"}
    return [
        {"requirements": [], "supports": [], "links": [], "rationale": "free text"},
        {"requirements": [], "supports": [], "links": []},
        {
            "requirements": [{"question_quote": "not in the question", "kind": "location"}],
            "supports": [],
            "links": [],
        },
        {"requirements": [{**grounded, "kind": "x"}], "supports": [], "links": []},
        {
            "requirements": [grounded],
            "supports": [_support(0, 99, "I stored the amber key in drawer three.", "direct")],
            "links": [],
        },
        {
            "requirements": [grounded],
            "supports": [_support(0, 0, "I stored the amber key in drawer three.", "direct")],
            "links": [_link(0, 0, "x", "y", "same_source_context")],
        },
    ]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({"requirements": [], "supports": [], "links": []}, "empty_requirements"),
        (
            {
                "requirements": [{"question_quote": "not in the question", "kind": "location"}],
                "supports": [],
                "links": [],
            },
            "requirement_quote_paraphrased",
        ),
        (
            {
                "requirements": [
                    {"question_quote": "Where did I store the amber key", "kind": "location"}
                ],
                "supports": [
                    _support(0, 99, "I stored the amber key in drawer three.", "direct")
                ],
                "links": [],
            },
            "support_index_out_of_range",
        ),
        (
            {
                "requirements": [
                    {"question_quote": "Where did I store the amber key", "kind": "location"}
                ],
                "supports": [],
                "links": [_link(0, 0, "x", "y", "same_source_context")],
            },
            "link_self_reference",
        ),
        (
            {
                "requirements": [
                    {"question_quote": "Where did I store the amber key", "kind": "location"}
                ],
                "supports": [],
                "links": [
                    _link(0, 1, "x", "y", "same_source_context") for _ in range(25)
                ],
            },
            "link_count_exceeded",
        ),
        (
            {"requirements": [], "supports": [], "links": [], "rationale": "free text"},
            "schema_invalid",
        ),
    ],
)
def test_structural_failures_report_a_fixed_reason(
    response: Mapping[str, object], reason: str
) -> None:
    llm = FakeStructuredLLM(
        [dict(response), {"evidence_state": "sufficient", "selected_indices": [0]}]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    second = _evidence(2, "source-b", "The amber key is in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct, second],
        _anchors(direct, second),
        GraphContext(),
    )

    assert outcome.graph.fallback is True
    assert outcome.graph.failure_category == "planner_invalid"
    assert outcome.graph.planner_failure_reason == reason


def test_successful_proposals_report_no_failure_reason() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[
                    _support(0, 0, "I stored the amber key in drawer three.", "direct")
                ],
            )
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct],
        _anchors(direct),
        GraphContext(),
    )

    assert outcome.graph.fallback is False
    assert outcome.graph.planner_failure_reason == "none"


@pytest.mark.parametrize("response", _invalid_proposals())
def test_structurally_invalid_proposals_fall_back_once(response: Mapping[str, object]) -> None:
    llm = FakeStructuredLLM(
        [dict(response), {"evidence_state": "sufficient", "selected_indices": [0]}]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct],
        _anchors(direct),
        GraphContext(),
    )

    assert outcome.graph.fallback is True
    assert outcome.graph.failure_category == "planner_invalid"
    assert outcome.selection.evidence == (direct,)
    assert [request.prompt_version for request in llm.requests] == [
        "evidence-graph-planner-v2",
        "evidence-selector-v7",
    ]


def test_ungrounded_support_quote_is_dropped_without_a_fallback() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[
                    _support(0, 0, "I stored the amber key in drawer three.", "direct"),
                    _support(0, 1, "I stored the amber key in drawer nine.", "direct"),
                ],
            )
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    other = _evidence(2, "source-b", "The shed holds bicycles.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct, other],
        _anchors(direct, other),
        GraphContext(),
    )

    assert outcome.graph.support_count == 1
    assert outcome.graph.fallback is False
    assert outcome.selection.evidence == (direct,)
    assert len(llm.requests) == 1


def test_valid_insufficient_graph_never_falls_back() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Which city hosts the workshop", "location")],
                supports=[],
            )
        ]
    )
    noise = _evidence(1, "source-a", "Nora likes workshops.")

    outcome = _selector(llm).select(
        "Which city hosts the workshop that Nora attended?",
        None,
        [noise],
        _anchors(noise),
        GraphContext(),
    )

    assert outcome.selection.evidence == ()
    assert outcome.selection.evidence_state == "insufficient"
    assert outcome.selection.abstained is True
    assert outcome.graph.fallback is False
    assert outcome.graph.state == "insufficient"
    assert len(llm.requests) == 1


def test_pure_image_query_keeps_the_existing_path() -> None:
    llm = FakeStructuredLLM([])
    anchor = _evidence(1, "source-a", "A photograph of a key.", granularity="message")

    outcome = _selector(llm).select(
        _IMAGE_QUERY,
        None,
        [anchor],
        _anchors(anchor),
        GraphContext(),
    )

    assert llm.requests == []
    assert outcome.graph.planner_calls == 0
    assert outcome.graph.fallback is False
    assert outcome.selection.evidence == (anchor,)


def test_pure_image_query_preserves_user_scoped_anchor_beyond_graph_pool() -> None:
    llm = FakeStructuredLLM([])
    ranked = [
        _evidence(index, f"source-{index}", f"Image candidate {index}.")
        for index in range(1, 34)
    ]
    deep_anchor = ranked[-1]

    outcome = _selector(llm).select(
        _IMAGE_QUERY,
        None,
        ranked,
        _anchors(deep_anchor),
        GraphContext(),
    )

    assert llm.requests == []
    assert outcome.selection.evidence == (deep_anchor,)


def test_unexpected_internal_failure_fails_closed_to_the_frozen_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("internal")

    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[_support(0, 0, "I stored the amber key in drawer three.", "direct")],
            ),
            {"evidence_state": "sufficient", "selected_indices": [0]},
        ]
    )
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    monkeypatch.setattr("masm.retrieval.evidence_graph.assemble", _boom)

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [direct],
        _anchors(direct),
        GraphContext(),
    )

    assert outcome.graph.failure_category == "internal_error"
    assert outcome.graph.fallback is True
    assert outcome.selection.evidence == (direct,)


# --- 20. telemetry-level isolation -------------------------------------------------------


def test_candidates_from_another_user_never_enter_the_graph_pool() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[_support(0, 0, "I stored the amber key in drawer three.", "direct")],
            )
        ]
    )
    mine = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    foreign = _evidence(2, "source-b", "Foreign memory text.", user_id="other-user")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [mine, foreign],
        _anchors(mine, foreign),
        GraphContext(),
    )

    assert outcome.graph.candidate_count == 1
    assert outcome.selection.evidence == (mine,)
    assert "Foreign memory text." not in json.dumps(llm.requests[0].payload, ensure_ascii=False)


def test_request_user_scope_is_not_inferred_from_first_ranked_candidate() -> None:
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("Where did I store the amber key", "location")],
                supports=[_support(0, 0, "I stored the amber key in drawer three.", "direct")],
            )
        ]
    )
    foreign = _evidence(1, "source-b", "Foreign memory text.", user_id="other-user")
    mine = _evidence(2, "source-a", "I stored the amber key in drawer three.")

    outcome = _selector(llm).select(
        "Where did I store the amber key?",
        None,
        [foreign, mine],
        _anchors(foreign, mine),
        GraphContext(),
        request_user_id=_USER,
    )

    assert outcome.graph.candidate_count == 1
    assert outcome.selection.evidence == (mine,)
    rendered = json.dumps(llm.requests[0].payload, ensure_ascii=False)
    assert "Foreign memory text." not in rendered


# --- property-style determinism ----------------------------------------------------------


def test_selection_is_stable_when_unrelated_candidates_are_permuted() -> None:
    direct = _evidence(1, "source-a", "I stored the amber key in drawer three.")
    others = [
        _evidence(index + 2, f"source-{index:02d}", f"Unrelated note {index}.")
        for index in range(4)
    ]
    chains = [
        [direct, *others],
        [*others[:2], direct, *others[2:]],
        [*reversed(others), direct],
    ]
    selected: list[list[UUID]] = []
    for ranked in chains:
        llm = FakeStructuredLLM(
            [
                _proposal(
                    requirements=[_requirement("Where did I store the amber key", "location")],
                    supports=[
                        _support(
                            0,
                            ranked.index(direct),
                            "I stored the amber key in drawer three.",
                            "direct",
                        )
                    ],
                )
            ]
        )
        outcome = _selector(llm).select(
            "Where did I store the amber key?",
            None,
            ranked,
            _anchors(*ranked),
            GraphContext(),
        )
        selected.append([item.memory_id for item in outcome.selection.evidence])

    assert selected == [[direct.memory_id]] * 3


def test_cycles_and_diamonds_terminate_with_one_component() -> None:
    question = "What is the code name and when is the review?"
    first = _evidence(1, "source-a", "The Zephyr code was chosen.", position=0)
    second = _evidence(2, "source-a", "The Zephyr review is in April.", position=1)
    third = _evidence(3, "source-a", "The Zephyr review notes are stored.", position=2)
    cyclic = _proposal(
        requirements=[
            _requirement("What is the code name", "attribute"),
            _requirement("when is the review", "time"),
        ],
        supports=[
            _support(0, 0, "The Zephyr code was chosen.", "direct"),
            _support(1, 1, "The Zephyr review is in April.", "direct"),
            _support(1, 2, "The Zephyr review notes are stored.", "context"),
        ],
        links=[
            _link(
                0,
                1,
                "The Zephyr code was chosen.",
                "The Zephyr review is in April.",
                "same_source_context",
            ),
            _link(
                1,
                2,
                "The Zephyr review is in April.",
                "The Zephyr review notes are stored.",
                "same_source_context",
            ),
            _link(
                0,
                2,
                "The Zephyr code was chosen.",
                "The Zephyr review notes are stored.",
                "same_source_context",
            ),
        ],
    )
    llm = FakeStructuredLLM([cyclic, cyclic, cyclic])

    outcomes = [
        _selector(llm).select(
            question,
            None,
            [first, second, third],
            _anchors(first, second, third),
            GraphContext(source_context_enabled=True),
        )
        for _ in range(3)
    ]

    for outcome in outcomes:
        assert outcome.graph.component_count == 1
        assert outcome.selection.evidence_state == "sufficient"
        assert outcome.selection.evidence == (first, second, third)
    assert outcomes[0].selection.evidence == outcomes[2].selection.evidence
def test_named_subject_present_in_the_store_keeps_the_evidence() -> None:
    stored = _evidence(1, "source-a", "The access code for the lockB-77 locker is code-7.")
    baseline = _StubBaseline((stored,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What is the access code", "location")],
                supports=[_support(0, 0, "The access code for the lockB-77 locker", "direct")],
            )
        ]
    )

    selector = GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        baseline,  # type: ignore[arg-type]
        additive=True,
        # 库里存在这个主体（即使它没被召回进候选池）时绝不允许拒答。
        presence=lambda user_id, tokens: frozenset(tokens),
    )
    outcome = selector.select(
        "What is the access code for the lockB-77 locker?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.graph.vetoed is False
    assert outcome.selection.evidence == (stored,)
# --- R11: append the bridging session when the question names only one side ------------------


def test_additive_mode_appends_the_bridging_session() -> None:
    sister = _evidence(1, "source-a", "My sister Nora works at the Zephyr office.")
    party = _evidence(2, "source-b", "The Zephyr holiday party is on 14 December.")
    baseline = _StubBaseline((party,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("When is my sister's company holiday party", "time")],
                supports=[_support(0, 0, "holiday party is on 14 December", "direct")],
            )
        ]
    )

    outcome = _additive_selector(llm, baseline).select(
        "When is my sister's company holiday party?",
        None,
        [sister, party],
        _anchors(sister, party),
        GraphContext(),
        request_user_id="user-1",
    )

    ids = [item.memory_id for item in outcome.selection.evidence]
    assert ids[0] == party.memory_id
    # 桥接证据被追加：问题里的 "sister" 与已选证据里的稀有单元 Zephyr 由它连接。
    assert sister.memory_id in ids


def test_additive_mode_does_not_append_when_sessions_already_span_two() -> None:
    sister = _evidence(1, "source-a", "My sister Nora works at the Zephyr office.")
    party = _evidence(2, "source-b", "The Zephyr holiday party is on 14 December.")
    third = _evidence(3, "source-c", "The Zephyr office has twelve desks.")
    baseline = _StubBaseline((party, sister))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("When is my sister's company holiday party", "time")],
                supports=[_support(0, 0, "holiday party is on 14 December", "direct")],
            )
        ]
    )

    outcome = _additive_selector(llm, baseline).select(
        "When is my sister's company holiday party?",
        None,
        [sister, party, third],
        _anchors(sister, party, third),
        GraphContext(),
        request_user_id="user-1",
    )

    assert [item.memory_id for item in outcome.selection.evidence] == [
        party.memory_id,
        sister.memory_id,
    ]
def test_named_subject_absent_without_a_sibling_frame_keeps_the_evidence() -> None:
    stored = _evidence(1, "source-a", "My sister Nora works at the office as an architect.")
    baseline = _StubBaseline((stored,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What did Alice say", "location")],
                supports=[_support(0, 0, "My sister Nora works at the office", "context")],
            )
        ]
    )

    selector = GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        baseline,  # type: ignore[arg-type]
        additive=True,
        # 用户自己的名字不在库里，但库里没有"同框架、另一个名字"的记忆 → 不得拒答。
        presence=lambda user_id, tokens: frozenset(),
        frames=lambda user_id, question: (),
    )
    outcome = selector.select(
        "What did Alice say about her sister?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.graph.vetoed is False
    assert outcome.selection.evidence == (stored,)
def test_relation_prefixed_absent_name_is_refused_without_a_template() -> None:
    stored = _evidence(1, "source-a", "My sister Nora works at the office as an architect.")
    baseline = _StubBaseline((stored,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[_requirement("What does my sister Maya do", "location")],
                supports=[_support(0, 0, "My sister Nora works at the office", "context")],
            )
        ]
    )

    selector = GraphEvidenceSelector(
        EvidenceGraphPlanner(llm),
        baseline,  # type: ignore[arg-type]
        additive=True,
        # 关系前缀名 Maya 全库缺席 → 判据 A 生效，无需模板近失配。
        presence=lambda user_id, tokens: frozenset(),
        frames=lambda user_id, question: (),
    )
    outcome = selector.select(
        "What does my sister Maya do for work?",
        None,
        [stored],
        _anchors(stored),
        GraphContext(),
        request_user_id="user-1",
    )

    assert outcome.graph.vetoed is True
    assert outcome.selection.evidence == ()
def test_additive_mode_appends_a_planner_linked_candidate() -> None:
    first = _evidence(1, "source-a", "The onboarding handbook covers badge pickup.")
    second = _evidence(2, "source-b", "New starters collect passes at the north gate.")
    baseline = _StubBaseline((first,))
    llm = FakeStructuredLLM(
        [
            _proposal(
                requirements=[
                    _requirement("Where do new starters collect", "location"),
                    _requirement("their passes", "attribute"),
                ],
                supports=[
                    _support(0, 1, "New starters collect passes at the north gate.", "direct")
                ],
                links=[
                    _link(
                        0,
                        1,
                        "The onboarding handbook covers badge pickup.",
                        "New starters collect passes at the north gate.",
                        "persisted_relation",
                    )
                ],
            )
        ]
    )

    outcome = _additive_selector(llm, baseline).select(
        "Where do new starters collect their passes?",
        None,
        [first, second],
        _anchors(first, second),
        GraphContext(relation_source_pairs=(("source-a", "source-b"),)),
        request_user_id="user-1",
    )

    ids = [item.memory_id for item in outcome.selection.evidence]
    # planner 声明并校验过的 link 让第二会话被追加，即使两段文字没有共同词面单元。
    assert first.memory_id in ids
    assert second.memory_id in ids
    assert outcome.graph.link_added >= 1
