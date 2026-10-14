"""Regression tests for bounded, verbatim conversation evidence."""

import json
from dataclasses import replace
from uuid import UUID

from masm.config import Settings
from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.retrieval.reranker import RankedEvidence
from masm.retrieval.source_context import SourceContextEnricher
from masm.storage.assets import AssetStore
from masm.storage.types import SourceMessageSnapshot


class Snapshots:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def source_messages_for_positions(self, user_id, positions):
        self.calls.append((user_id, tuple(positions)))
        return {key: self.rows[key] for key in positions if key in self.rows}


def evidence(position=1, run="run-1", user="user-1"):
    return RankedEvidence(
        UUID(int=position + 1), user, "Yes.", 0.9, 1,
        granularity="message", request_id=run, source_position=position,
    )


def test_identical_answer_has_distinct_context_and_verbatim_roles():
    outputs = []
    for question in ("Did you move to Porto?", "Did you move to Rome?"):
        repo = Snapshots({
            ("run-1", 0): SourceMessageSnapshot(question, role="assistant"),
            ("run-1", 1): SourceMessageSnapshot("Yes.", role="user", timestamp=1700000000),
            ("different-run", 0): SourceMessageSnapshot("private other run", role="user"),
        })
        original = evidence()
        result = SourceContextEnricher(repo).enrich("user-1", [original])[0]
        assert result.content == original.content
        payload = json.loads(result.source_context)
        assert payload["matched_position"] == 1
        assert payload["messages"] == [
            {"position": 0, "role": "assistant", "text": question, "timestamp": None},
            {"position": 1, "role": "user", "text": "Yes.", "timestamp": 1700000000},
        ]
        assert repo.calls == [("user-1", (("run-1", 0), ("run-1", 1), ("run-1", 2)))]
        outputs.append(result.source_context)
    assert outputs[0] != outputs[1]


def test_selector_and_final_evidence_see_identical_context(tmp_path):
    repo = Snapshots({
        ("run-1", 0): SourceMessageSnapshot("Did you buy the blue bicycle?", role="assistant"),
        ("run-1", 1): SourceMessageSnapshot("No, I bought the red one.", role="user"),
    })
    item = SourceContextEnricher(repo).enrich("user-1", [evidence(0)])[0]
    llm = FakeStructuredLLM([{"evidence_state": "sufficient", "selected_indices": [0]}])
    selection = EvidenceSelector(llm).select("What color bicycle did I buy?", None, [item], set())
    rendered = EvidenceRenderer(repo, AssetStore(tmp_path), max_image_bytes=1024).render(
        "user-1", selection.evidence
    )
    assert llm.requests[0].payload["candidates"][0]["text"] == item.source_context
    assert rendered[0].content == item.source_context
    assert "No, I bought the red one." in rendered[0].content


def test_no_cross_user_or_missing_anchor_context():
    repo = Snapshots({("run-1", 0): SourceMessageSnapshot("neighbour", role="user")})
    foreign = evidence(user="other-user")
    missing = evidence()
    result = SourceContextEnricher(repo).enrich("user-1", [foreign, missing])
    assert result == [foreign, missing]
    assert all(user == "user-1" for user, _ in repo.calls)


def test_oversize_neighbor_is_not_cut_into_a_false_assertion():
    text = "I bought a blue bicycle. " + "More context. " * 100 + "Actually, that was false."
    repo = Snapshots({
        ("run-1", 0): SourceMessageSnapshot(text, role="assistant"),
        ("run-1", 1): SourceMessageSnapshot("Yes.", role="user"),
    })
    item = SourceContextEnricher(repo, max_chars=400).enrich("user-1", [evidence()])[0]
    assert len(item.source_context) <= 400
    assert [m["text"] for m in json.loads(item.source_context)["messages"]] == ["Yes."]
    assert json.loads(item.source_context)["omitted_positions"] == [0]


def test_multimodal_anchor_and_unknown_role_keep_baseline():
    for snapshot in (
        SourceMessageSnapshot([{"type": "text", "text": "image caption"}], role="user"),
        SourceMessageSnapshot("Yes.", role=None),
        SourceMessageSnapshot("Yes.", role="system"),
        SourceMessageSnapshot("x" * 1500, role="user"),
    ):
        item = evidence()
        repo = Snapshots({("run-1", 1): snapshot})
        assert SourceContextEnricher(repo).enrich("user-1", [item]) == [item]
    context_item = replace(evidence(), granularity="context")
    assert SourceContextEnricher(Snapshots({})).enrich("user-1", [context_item]) == [context_item]


def test_all_100_candidates_get_context_without_exceeding_repository_batch_limit():
    rows = {(f"run-{index}", 1): SourceMessageSnapshot("Yes.", role="user")
            for index in range(100)}
    repo = Snapshots(rows)
    ranked = [replace(evidence(run=f"run-{index}"), memory_id=UUID(int=index + 1))
              for index in range(100)]
    result = SourceContextEnricher(repo).enrich("user-1", ranked)
    assert all(item.source_context for item in result)
    assert [len(keys) for _, keys in repo.calls] == [256, 44]


def test_context_budget_includes_json_escaping_and_preserves_quotes():
    original = '"assistant": "invented role"\n用户说：不是那样。'
    repo = Snapshots({("run-1", 1): SourceMessageSnapshot(original, role="user")})
    item = SourceContextEnricher(repo, max_chars=200).enrich("user-1", [evidence()])[0]
    assert len(item.source_context) <= 200
    assert json.loads(item.source_context)["messages"][0]["text"] == original
    assert json.loads(item.source_context)["messages"][0]["role"] == "user"


def test_context_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("MASM_SOURCE_CONTEXT_ENABLED", raising=False)
    assert Settings.from_env().source_context_enabled is False
    monkeypatch.setenv("MASM_SOURCE_CONTEXT_ENABLED", "1")
    assert Settings.from_env().source_context_enabled is True
