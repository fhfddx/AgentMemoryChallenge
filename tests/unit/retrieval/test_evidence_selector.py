"""Structured selection returns original evidence or a safe empty/fallback result."""

import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import pytest

from masm.providers.fakes import FakeStructuredLLM
from masm.providers.llm import ModelUnavailableError, StructuredOutputError
from masm.retrieval.anchor_connectivity import AnchorConnectivity
from masm.retrieval.evidence_selector import (
    DeterministicEvidenceSelector,
    EvidenceSelection,
    EvidenceSelector,
)
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ImageURLPart, TextPart
from masm.storage.types import MemoryCandidate


def _evidence(number: int, source: str, content: str) -> RankedEvidence:
    return RankedEvidence(
        memory_id=UUID(int=number),
        user_id="user-1",
        content=content,
        score=1.0 / number,
        rank=number,
        request_id=source,
        granularity="message",
        source_position=number - 1,
        metadata={"lexical": 0.05},
    )


def test_direct_fact_selection_returns_the_original_evidence_object() -> None:
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": True}], model="gpt-4o-mini"
    )
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")
    irrelevant = _evidence(2, "source-b", "Alice visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", ["Notebook", "Paris"],
        [direct, irrelevant], {direct.memory_id, irrelevant.memory_id},
    )

    assert result.evidence == (direct,)
    assert result.candidate_count == 2
    assert result.source_count == 2
    assert result.selected_source_count == 1
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 1
    assert llm.requests[0].max_attempts == 1
    assert llm.requests[0].prompt_version == "evidence-selector-v7"
    assert llm.requests[0].payload["question"] == "What did Alice buy?"
    assert llm.requests[0].payload["options"] == ["Notebook", "Paris"]


def test_insufficient_decision_without_indices_abstains_without_fallback() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    purchase = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?",
        None,
        [purchase, unrelated],
        set(),
    )

    assert result.evidence == ()
    assert result.fallback is False
    assert result.abstained is True
    assert result.evidence_state == "insufficient"


def test_inconsistent_insufficient_decision_normalizes_to_safe_abstention() -> None:
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": False}]
    )
    anchor = _evidence(1, "source-a", "Alice bought a blue notebook")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [anchor], {anchor.memory_id}
    )

    assert result.evidence == ()
    assert result.fallback is False
    assert result.abstained is True
    assert result.failure_category == "invalid_output"
    assert result.evidence_state == "insufficient"


def test_partial_direct_evidence_is_retained_instead_of_being_discarded() -> None:
    """成对用例 A：模型声明 partial 并给出直接事实，不得丢弃全部召回。

    `partial` 表示「选中的记忆直接陈述了某个被询问的事实，但不足以覆盖全部询问」。
    这是二态协议无法表达的边界：既不能无条件拒答，也不能回落到旧式全量强锚点。
    """
    llm = FakeStructuredLLM([{"selected_indices": [0], "evidence_state": "partial"}])
    purchase = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?",
        None,
        [purchase, unrelated],
        {purchase.memory_id, unrelated.memory_id},
    )

    assert result.evidence == (purchase,)
    assert result.abstained is False
    assert result.fallback is False
    assert result.evidence_state == "partial"
    assert result.failure_category == "none"


def test_missing_link_evidence_still_abstains_without_unconditional_fallback() -> None:
    """成对用例 B：候选只是局部推理链，必须拒答，不得恢复旧式无条件回退。

    模型明确声明 `insufficient` 时，即使 indices 非空也必须拒答；旧行为会把这类
    矛盾输出回落到全量 question-admitted 强锚点，从而在证据不足时仍然作答。
    """
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "evidence_state": "insufficient"}]
    )
    local_link = _evidence(1, "source-a", "Nora plans to attend the Zephyr workshop")
    receipt = _evidence(2, "source-b", "Nora filed a travel receipt on Tuesday")
    unrelated = _evidence(3, "source-c", "The training room has a blue clock")

    result = EvidenceSelector(llm).select(
        "In which city is the workshop that Nora plans to attend?",
        None,
        [local_link, receipt, unrelated],
        {item.memory_id for item in (local_link, receipt, unrelated)},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def test_partial_without_usable_indices_abstains_instead_of_falling_back() -> None:
    """partial 但没有可用索引时没有可引用事实，按拒答处理而不是强锚点回退。"""
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "partial"}])
    anchor = _evidence(1, "source-a", "the needed fact")

    result = EvidenceSelector(llm).select(
        "the needed fact", None, [anchor], {anchor.memory_id}
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


@pytest.mark.parametrize("state", ["partial", "sufficient"])
def test_selection_without_explicit_question_anchor_abstains(
    state: Literal["partial", "sufficient"],
) -> None:
    """选择结果缺少问题中的显式实体时，不能返回同主题的其他事实。"""
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "evidence_state": state}]
    )
    chain_fragment = _evidence(
        1,
        "chain",
        "The terminal record associated with Leaf-case is Terminal-case.",
    )
    isolated = _evidence(
        2,
        "isolated",
        "Unconnected-case belongs to a separate archive.",
    )

    result = EvidenceSelector(llm).select(
        "What terminal record is linked to Unconnected-case?",
        None,
        [chain_fragment, isolated],
        {chain_fragment.memory_id, isolated.memory_id},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def _chain_pool() -> list[RankedEvidence]:
    """云端探针的四个 run：三条链记录 + 一条同主题但无关系边的隔离记录。"""
    return [
        _evidence(
            1, "run-root", "The registered chain Root-case continues through Bridge-case."
        ),
        _evidence(
            2,
            "run-bridge",
            "For the same registered chain, Bridge-case continues through Leaf-case.",
        ),
        _evidence(
            3, "run-leaf", "The terminal record associated with Leaf-case is Terminal-case."
        ),
        _evidence(
            4,
            "run-isolated",
            "The isolated catalog entry Unconnected-case uses registered-chain terminology "
            "but belongs to a separate archive.",
        ),
    ]


class _ChainGraph:
    """只含 run-root -> run-bridge -> run-leaf 的假关系图；隔离 run 没有任何边。"""

    _EDGES = {1: 2, 2: 3}
    _RUNS = {1: "run-root", 2: "run-bridge", 3: "run-leaf", 4: "run-isolated"}

    def __init__(self) -> None:
        self.related_calls: list[list[UUID]] = []
        self.context_calls: list[list[str]] = []

    def _context(self, number: int) -> MemoryCandidate:
        return MemoryCandidate(
            memory_id=UUID(int=100 + number),
            user_id="user-1",
            content="context",
            score=0.0,
            request_id=self._RUNS[number],
        )

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[MemoryCandidate]:
        del user_id
        self.context_calls.append(list(request_ids))
        return [
            self._context(number)
            for number, run in self._RUNS.items()
            if run in request_ids
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        del user_id
        self.related_calls.append(list(memory_ids))
        neighbours = [
            self._context(self._EDGES[memory_id.int - 100])
            for memory_id in memory_ids
            if memory_id.int - 100 in self._EDGES
        ]
        return neighbours[:limit]


class _NoRelations:
    """关系图为空的假仓库：复现云端「related 找不到任何邻居」的事实。"""

    def __init__(self) -> None:
        self.context_calls: list[list[str]] = []

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[MemoryCandidate]:
        del user_id
        self.context_calls.append(list(request_ids))
        return [
            MemoryCandidate(
                memory_id=UUID(int=300 + index),
                user_id="user-1",
                content="context",
                score=0.0,
                request_id=run,
            )
            for index, run in enumerate(request_ids)
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        del user_id, memory_ids, limit
        return []


class _SingleEdgeGraph:
    """只有 run-a 的 context 与 run-b 的 context 之间一条边。"""

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                memory_id=UUID(int=200 + index),
                user_id=user_id,
                content="context",
                score=0.0,
                request_id=run,
            )
            for index, run in enumerate(request_ids)
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        del limit
        if all(memory_id.int != 200 for memory_id in memory_ids):
            return []
        return [
            MemoryCandidate(
                memory_id=UUID(int=201),
                user_id=user_id,
                content="context",
                score=0.0,
                request_id="run-b",
            )
        ]


def _connected_selector(
    llm: FakeStructuredLLM, connectivity: AnchorConnectivity
) -> EvidenceSelector:
    return EvidenceSelector(llm, anchor_connectivity=connectivity)


def test_identifier_linked_chain_is_kept_when_the_relation_graph_is_empty() -> None:
    """云端回归的最小复现：关系图为空时，共享标识符相连的下游链必须整链保留。

    c7eede8 在线上的 round 1 里，模型原始选择与未接线时逐字相同（`[0,1,2]`、sufficient），
    但接入关系可达性后只返回 1 条（2/4 标记），因为 `related()` 没有找回任何邻居。合法下游
    证据是靠共享标识符（Bridge-case、Leaf-case）串起来的，不能因为关系图为空就丢弃。
    """
    llm = FakeStructuredLLM([{"selected_indices": [0, 1, 2], "evidence_state": "sufficient"}])
    pool = _chain_pool()

    result = _connected_selector(llm, AnchorConnectivity(_NoRelations())).select(
        "Follow the registered chain from Root-case and identify its terminal record.",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[0], pool[1], pool[2])
    assert result.evidence_state == "sufficient"
    assert result.abstained is False


def test_mixed_selection_is_rejected_without_any_relation_oracle() -> None:
    """标识符连通判定不依赖注入的关系图：未接线时也必须拦住混合过选。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 3], "evidence_state": "partial"}])
    pool = _chain_pool()

    result = EvidenceSelector(llm).select(
        "What terminal record is linked to Unconnected-case?",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[3],)
    assert result.abstained is False


def test_relation_edge_links_selected_evidence_without_shared_identifiers() -> None:
    """关系边是额外的边来源：两条证据没有共同标识符时仍可按关系连通保留。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 1], "evidence_state": "sufficient"}])
    anchor = _evidence(1, "run-a", "Root-case is recorded here.")
    unrelated_wording = _evidence(2, "run-b", "A separate archive note with no shared terms.")

    result = _connected_selector(
        llm, AnchorConnectivity(_SingleEdgeGraph())
    ).select(
        "What does Root-case describe?",
        None,
        [anchor, unrelated_wording],
        {anchor.memory_id, unrelated_wording.memory_id},
    )

    assert result.evidence == (anchor, unrelated_wording)
    assert result.evidence_state == "sufficient"


def test_identifier_linked_chain_beyond_the_relation_hops_is_kept() -> None:
    """链路比关系跳数上限更长时，标识符相邻仍必须让整条链留下。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 1, 2], "evidence_state": "sufficient"}])
    pool = _chain_pool()

    result = _connected_selector(llm, AnchorConnectivity(_NoRelations())).select(
        "Follow the registered chain from Root-case and identify its terminal record.",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[0], pool[1], pool[2])


def test_mixed_selection_of_anchor_and_unrelated_chain_evidence_is_rejected() -> None:
    """云端失败轮的最小复现：锚点证据与无关链证据混合选择时必须剔除无关链。

    第三轮 `multi_session_path_probe` 的 stage_selected 返回 partial、selected_count=2、
    matched_marker_count=1、forbidden_marker_count=2。整组选中文本的并集确实包含查询锚点
    `Unconnected-case`（隔离记录自带），所以集合级校验会放行；但另一条选中的链头证据与
    锚点没有任何关系边，必须不能出现在结果里。
    """
    llm = FakeStructuredLLM([{"selected_indices": [0, 3], "evidence_state": "partial"}])
    pool = _chain_pool()
    graph = _ChainGraph()

    result = _connected_selector(llm, AnchorConnectivity(graph)).select(
        "What terminal record is linked to Unconnected-case?",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[3],)
    assert result.selected_source_count == 1
    assert result.abstained is False
    assert result.fallback is False
    assert result.evidence_state == "partial"


def test_connected_multi_hop_selection_keeps_every_downstream_link() -> None:
    """合法链的回归护栏：下游证据不重复 Root 字符串，也必须整链保留。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 1, 2], "evidence_state": "sufficient"}])
    pool = _chain_pool()
    graph = _ChainGraph()

    result = _connected_selector(llm, AnchorConnectivity(graph)).select(
        "Follow the registered chain from Root-case and identify its terminal record.",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[0], pool[1], pool[2])
    assert result.evidence_state == "sufficient"
    assert result.abstained is False


def test_sufficient_selection_with_an_unrelated_extra_downgrades_to_partial() -> None:
    """剔除未接地证据后，模型「完整覆盖」的断言不再成立，只能降级为 partial。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 3], "evidence_state": "sufficient"}])
    pool = _chain_pool()

    result = _connected_selector(llm, AnchorConnectivity(_ChainGraph())).select(
        "What terminal record is linked to Unconnected-case?",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[3],)
    assert result.evidence_state == "partial"
    assert result.fallback is False


def test_unrelated_chain_evidence_alone_still_abstains() -> None:
    """整组都不含锚点时保持既有拒答行为，且不触发关系读取。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 1], "evidence_state": "partial"}])
    pool = _chain_pool()
    graph = _ChainGraph()

    result = _connected_selector(llm, AnchorConnectivity(graph)).select(
        "What terminal record is linked to Unconnected-case?",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.evidence_state == "insufficient"
    assert graph.related_calls == []


def test_selection_without_explicit_anchors_skips_relation_reads() -> None:
    """自然语言问题没有结构化锚点时不做任何关系读取，行为完全不变。"""
    llm = FakeStructuredLLM([{"selected_indices": [0, 1], "evidence_state": "sufficient"}])
    pool = _chain_pool()
    graph = _ChainGraph()

    result = _connected_selector(llm, AnchorConnectivity(graph)).select(
        "Which records belong to the registered chain?",
        None,
        pool,
        {item.memory_id for item in pool},
    )

    assert result.evidence == (pool[0], pool[1])
    assert result.evidence_state == "sufficient"
    assert graph.related_calls == []
    assert graph.context_calls == []


def test_legacy_two_field_output_is_accepted_and_kept_on_the_safe_side() -> None:
    """旧 Provider 只有布尔位，无法表达 partial，因此矛盾输出保持拒答。"""
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": False}])
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")
    unrelated = _evidence(2, "source-b", "Bob visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy and where did she store it?", None,
        [direct, unrelated], {direct.memory_id, unrelated.memory_id},
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert result.evidence_state == "insufficient"
    assert result.failure_category == "invalid_output"


def test_legacy_two_field_sufficient_output_still_selects_evidence() -> None:
    """旧 Provider 的 sufficient_evidence=true 仍映射为 sufficient，行为不变。"""
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": True}])
    direct = _evidence(1, "source-a", "Alice bought a blue notebook")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [direct], {direct.memory_id}
    )

    assert result.evidence == (direct,)
    assert result.abstained is False
    assert result.fallback is False
    assert result.evidence_state == "sufficient"


def test_selector_requests_deterministic_three_state_decision() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    entity_only = _evidence(1, "source-a", "Alice stored a notebook in cabinet seven")

    EvidenceSelector(llm).select(
        "What is the notebook serial number?", None,
        [entity_only], {entity_only.memory_id},
    )

    request = llm.requests[0]
    assert request.temperature == 0.0
    assert request.prompt_version == "evidence-selector-v7"
    assert "smallest set of memories" in request.prompt
    assert "every attribute, relation, value, or event asked for" in request.prompt
    assert "distinct source_group" in request.prompt
    assert "evidence_state=partial" in request.prompt
    assert "evidence_state=insufficient" in request.prompt
    assert "local fragment of a longer reasoning chain" in request.prompt


def test_selector_prompt_requires_naming_a_stated_requested_fact() -> None:
    """v6 契约：partial 必须由「某条被选记忆直接陈述了被询问事实」支撑。

    v5 的合成探针显示模型会把仅共享实体/选项、或只是长链一环的候选报成 partial。
    提示词必须给出可执行的自检步骤，而不只是描述三种状态。
    """
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("fact", None, [candidate], {candidate.memory_id})

    request = llm.requests[0]
    assert request.prompt_version == "evidence-selector-v7"
    assert "Name the requested fact" in request.prompt
    assert "Never report partial merely because" in request.prompt
    assert "does not state the attribute the question asks for" in request.prompt
    assert "every link needed to connect them" in request.prompt


def test_selection_schema_asks_for_the_state_before_the_indices() -> None:
    """v7 契约：模型必须先决定 evidence_state，再生成 selected_indices。

    v6 探针在两轮里稳定出现 `insufficient` + 非空 indices。严格 JSON schema 的属性顺序
    是 selected_indices -> evidence_state，约束解码按属性顺序生成，所以模型在得出结论
    之前就已经写好了索引。把 evidence_state 提到第一位，等于让解码器先定状态。
    """
    assert list(EvidenceSelection.model_fields) == [
        "evidence_state",
        "selected_indices",
    ]
    assert list(EvidenceSelection.model_json_schema()["properties"]) == [
        "evidence_state",
        "selected_indices",
    ]

    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("fact", None, [candidate], {candidate.memory_id})

    request = llm.requests[0]
    assert request.prompt_version == "evidence-selector-v7"
    assert "Decide evidence_state before you choose selected_indices" in request.prompt


def test_selection_schema_accepts_only_the_two_protocol_fields() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "evidence_state": "insufficient"}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("fact", None, [candidate], {candidate.memory_id})

    assert EvidenceSelection.model_config["extra"] == "forbid"
    assert set(EvidenceSelection.model_fields) == {"selected_indices", "evidence_state"}
    assert EvidenceSelection.model_fields["evidence_state"].annotation == Literal[
        "sufficient", "partial", "insufficient"
    ]


def test_no_direct_evidence_decision_abstains_without_fallback() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    unrelated = _evidence(1, "source-a", "Alice visited Paris")

    result = EvidenceSelector(llm).select(
        "What did Alice buy?", None, [unrelated], {unrelated.memory_id}
    )

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False


def test_multi_source_question_keeps_both_selected_original_facts() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0, 1], "sufficient_evidence": True}])
    first = _evidence(1, "session-add-1", "Alice purchased a notebook")
    second = _evidence(2, "session-add-2", "Alice gave the notebook to Bob")

    result = EvidenceSelector(llm).select(
        "What did Alice give Bob after buying it?", None,
        [first, second], {first.memory_id, second.memory_id},
    )

    assert result.evidence == (first, second)
    assert result.selected_source_count == 2


def test_incomplete_first_pass_retries_with_deeper_candidates() -> None:
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [
            {"selected_indices": [], "sufficient_evidence": False},
            {"selected_indices": [0, 47], "sufficient_evidence": True},
        ]
    )

    result = EvidenceSelector(llm).select(
        "Which two facts complete the answer?", None, ranked, set()
    )

    assert result.evidence == (ranked[0], ranked[47])
    assert result.candidate_count == 48
    assert result.selected_source_count == 2
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 2
    assert len(llm.requests[0].payload["candidates"]) == 32
    assert len(llm.requests[1].payload["candidates"]) == 48


def test_partial_first_pass_retries_deeper_and_returns_the_complete_selection() -> None:
    """首轮只看到 32 个候选时给出的 partial，必须用更深候选池再试一次。

    partial 的含义就是「有直接事实但缺一部分」；缺的那部分可能落在 32 名之外。
    深层轮如果给出 sufficient，就必须采用它，而不是停在首轮的 partial。
    """
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [
            {"evidence_state": "partial", "selected_indices": [0]},
            {"evidence_state": "sufficient", "selected_indices": [0, 47]},
        ]
    )

    result = EvidenceSelector(llm).select(
        "Which two facts complete the answer?", None, ranked, set()
    )

    assert result.evidence == (ranked[0], ranked[47])
    assert result.evidence_state == "sufficient"
    assert result.candidate_count == 48
    assert result.selected_source_count == 2
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 2
    assert len(llm.requests[0].payload["candidates"]) == 32
    assert len(llm.requests[1].payload["candidates"]) == 48


@pytest.mark.parametrize(
    "deeper_output",
    [
        {"evidence_state": "insufficient", "selected_indices": []},
        {"evidence_state": "sufficient", "selected_indices": [99]},
        TimeoutError("private provider body"),
    ],
    ids=["insufficient", "invalid_output", "unavailable"],
)
def test_partial_first_pass_survives_a_worse_deeper_round(deeper_output) -> None:
    """深层重试只能改进结果，绝不能丢掉首轮已经安全的 partial 证据。"""
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [
            {"evidence_state": "partial", "selected_indices": [0]},
            deeper_output,
        ]
    )

    result = EvidenceSelector(llm).select(
        "Which facts complete the answer?",
        None,
        ranked,
        {item.memory_id for item in ranked},
    )

    assert result.evidence == (ranked[0],)
    assert result.evidence_state == "partial"
    assert result.abstained is False
    assert result.fallback is False
    assert result.failure_category == "none"
    assert len(llm.requests) == 2


def test_sufficient_first_pass_does_not_retry_deeper_candidates() -> None:
    ranked = [
        _evidence(number, f"source-{number}", f"fact {number}")
        for number in range(1, 49)
    ]
    llm = FakeStructuredLLM(
        [{"selected_indices": [0], "sufficient_evidence": True}]
    )

    result = EvidenceSelector(llm).select(
        "Which fact completes the answer?", None, ranked, set()
    )

    assert result.evidence == (ranked[0],)
    assert result.candidate_count == 32
    assert result.fallback is False
    assert result.abstained is False
    assert len(llm.requests) == 1


def test_atomic_fact_survives_even_when_its_rank_exceeds_response_cap() -> None:
    ranked = [_evidence(number, f"source-{number}", f"fact {number}") for number in range(1, 15)]
    llm = FakeStructuredLLM([{"selected_indices": [12], "sufficient_evidence": True}])

    result = EvidenceSelector(llm, max_candidates=14).select(
        "Which exact fact is needed?", None, ranked, {item.memory_id for item in ranked}
    )

    assert result.evidence == (ranked[12],)
    assert result.fallback is False
    assert result.candidate_count == 14


@pytest.mark.parametrize(
    ("question", "options", "content"),
    [
        ("What color was Alice's notebook?", ["blue", "green"], "Bob drove a blue car"),
        ("What did Alice buy?", None, "Alice visited Paris"),
    ],
)
def test_option_only_or_entity_only_memory_abstains(
    question: str, options: list[str] | None, content: str
) -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    candidate = _evidence(1, "source-a", content)

    result = EvidenceSelector(llm).select(question, options, [candidate], {candidate.memory_id})

    assert result.evidence == ()
    assert result.abstained is True
    assert result.fallback is False
    assert llm.requests[0].payload["question"] == question


def test_candidate_text_limit_cannot_be_configured_above_hard_cap() -> None:
    with pytest.raises(ValueError, match="max_chars_per_candidate"):
        EvidenceSelector(FakeStructuredLLM(), max_chars_per_candidate=4097)


def test_selector_payload_is_bounded_text_and_never_contains_image_bytes() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [0], "sufficient_evidence": True}])
    candidate = _evidence(1, "private-request-id", "X" * 1300)
    visual = ImageURLPart(image_url={"url": "data:image/png;base64,cHJpdmF0ZS1ieXRlcw=="})

    EvidenceSelector(llm).select(
        [TextPart(text="What is shown?"), visual], ["option"],
        [candidate], {candidate.memory_id},
    )

    payload = llm.requests[0].payload
    assert payload["visual_query_present"] is True
    assert payload["question"] == "What is shown?"
    assert len(payload["candidates"][0]["text"]) == 1200
    assert payload["candidates"][0]["source_group"] == "source-1"
    serialized = json.dumps(payload)
    assert "private-request-id" not in serialized
    assert "cHJpdmF0ZS1ieXRlcw==" not in serialized


def test_pure_visual_query_preserves_image_recall_without_uninformed_model_call() -> None:
    visual = ImageURLPart(image_url={"url": "data:image/png;base64,cHJpdmF0ZS1ieXRlcw=="})
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    anchor = _evidence(1, "image-request", "a blue bicycle beside a tree")
    relation_only = _evidence(2, "related-request", "unverified related context")

    result = EvidenceSelector(llm).select(
        [visual], None, [anchor, relation_only], {anchor.memory_id}
    )

    assert result.evidence == (anchor,)
    assert result.fallback is True
    assert result.failure_category == "none"
    assert result.abstained is False
    assert llm.requests == []


def test_empty_pool_abstains_without_model_request() -> None:
    llm = FakeStructuredLLM()

    result = EvidenceSelector(llm).select("unknown question", None, [], set())

    assert result.evidence == ()
    assert result.abstained is True
    assert llm.requests == []


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (ModelUnavailableError("private response"), "unavailable"),
        (TimeoutError("private response"), "unavailable"),
        (StructuredOutputError("private response"), "invalid_output"),
        ({"selected_indices": [True], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": ["bad"], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [-1], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [2], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [0, 0], "sufficient_evidence": True}, "invalid_output"),
        ({"selected_indices": [], "sufficient_evidence": True}, "invalid_output"),
    ],
)
def test_invalid_selection_falls_back_to_question_admitted_anchor(failure, category) -> None:
    llm = FakeStructuredLLM([failure])
    anchor = _evidence(1, "source-a", "the needed fact")
    relation_only = _evidence(2, "source-b", "relation expansion without recall signal")

    result = EvidenceSelector(llm).select(
        "needed fact", None, [anchor, relation_only], {anchor.memory_id}
    )

    assert result.evidence == (anchor,)
    assert result.fallback is True
    assert result.abstained is False
    assert result.failure_category == category
    assert len(llm.requests) == 1


def test_failed_selection_with_no_strong_anchor_abstains() -> None:
    candidate = _evidence(1, "source-a", "unrelated fact")
    selector = EvidenceSelector(FakeStructuredLLM([TimeoutError("private body")]))

    result = selector.select("missing fact", None, [candidate], set())

    assert result.evidence == ()
    assert result.fallback is True
    assert result.abstained is True


def test_over_limit_selection_falls_back_to_at_most_twelve_anchors() -> None:
    ranked = [_evidence(number, f"source-{number}", f"fact {number}") for number in range(1, 15)]
    llm = FakeStructuredLLM(
        [{"selected_indices": list(range(13)), "sufficient_evidence": True}]
    )

    result = EvidenceSelector(llm).select(
        "facts", None, ranked, {item.memory_id for item in ranked}
    )

    assert result.fallback is True
    assert len(result.evidence) == 12


def test_fallback_diversifies_sources_and_preserves_rank_order() -> None:
    ranked = [
        _evidence(1, "frequent-source", "fact 1"),
        _evidence(2, "frequent-source", "fact 2"),
        _evidence(3, "frequent-source", "fact 3"),
        _evidence(4, "frequent-source", "fact 4"),
        _evidence(5, "rare-source", "fact 5"),
    ]
    selector = EvidenceSelector(
        FakeStructuredLLM([TimeoutError()]), max_candidates=5, max_selected=4
    )

    result = selector.select("facts", None, ranked, {item.memory_id for item in ranked})

    assert [item.memory_id for item in result.evidence] == [
        UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=5)
    ]


def test_selector_limits_question_and_option_text_in_provider_payload() -> None:
    llm = FakeStructuredLLM([{"selected_indices": [], "sufficient_evidence": False}])
    candidate = _evidence(1, "source-a", "fact")

    EvidenceSelector(llm).select("Q" * 5000, ["O" * 800] * 30, [candidate], set())

    payload = llm.requests[0].payload
    assert len(payload["question"]) == 4000
    assert len(payload["options"]) == 16
    assert all(len(option) == 512 for option in payload["options"])


def test_deterministic_selector_passes_through_existing_local_fake_results() -> None:
    ranked = tuple(_evidence(number, "source-a", f"fact {number}") for number in range(1, 101))

    result = DeterministicEvidenceSelector().select("facts", None, ranked, set())

    assert result.evidence == ranked
    assert result.fallback is False
    assert result.abstained is False
