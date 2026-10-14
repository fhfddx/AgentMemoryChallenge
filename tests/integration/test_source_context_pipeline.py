"""Source identity survives the real storage path."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from masm.api.app import create_app
from masm.providers.fakes import FakeStructuredLLM
from masm.retrieval.evidence_selector import EvidenceSelector
from masm.schemas.api import AddRequest
from masm.services.deletion_service import DeletionService
from masm.storage.repositories import MemoryRepository


def test_source_snapshot_keeps_role_time_and_user_scope(
    database, add_service, asset_store,
) -> None:
    repository = MemoryRepository(database)
    run = f"source-context-{uuid4().hex}"
    users = [f"context-owner-{uuid4().hex}", f"context-other-{uuid4().hex}"]
    try:
        for user, role, text in zip(
            users, ["user", "assistant"], ["I live in Porto.", "Try visiting Rome."], strict=True
        ):
            add_service.add(AddRequest(
                user_id=user, request_id=run, session_id="context-session",
                messages=[{"role": role, "content": text, "timestamp": 1700000000}],
            ))
        own = repository.source_messages_for_positions(users[0], [(run, 0)])[(run, 0)]
        assert getattr(own, "role", None) == "user"
        assert getattr(own, "timestamp", None) == 1700000000
        assert own.content == "I live in Porto."
        assert repository.source_messages_for_positions("absent-user", [(run, 0)]) == {}
    finally:
        for user in users:
            assert DeletionService(repository, asset_store).delete_run(run, user_id=user).complete


@pytest.mark.parametrize("enabled", [False, True])
def test_search_response_preserves_the_context_seen_before_selection(
    database, asset_store, settings, embeddings, enabled,
):
    configured = replace(settings, source_context_enabled=enabled)
    app = create_app(configured, database=database, asset_store=asset_store, embeddings=embeddings)
    llm = FakeStructuredLLM([{"evidence_state": "sufficient", "selected_indices": [0, 1, 2]}])
    app.state.search_service._selector = EvidenceSelector(llm)
    user, run = f"context-{uuid4().hex}", f"run-{uuid4().hex}"
    repository = MemoryRepository(database)
    try:
        with TestClient(app) as client:
            added = client.post("/add", headers={"Authorization": "Bearer test-key"}, json={
                "user_id": user, "request_id": run, "session_id": "session",
                "messages": [
                    {"role": "assistant", "content": "Did you buy the blue bicycle?"},
                    {"role": "user", "content": "No, I bought the red one."},
                ],
            })
            assert added.status_code == 200
            found = client.post("/search", headers={"Authorization": "Bearer test-key"}, json={
                "user_id": user, "query": "What bicycle did I buy?", "top_k": 100,
            })
            assert found.status_code == 200
            texts = [item["content"] for item in found.json()["data"]]
            candidate_texts = [item["text"] for item in llm.requests[0].payload["candidates"]]
            if enabled:
                windows = [
                    json.loads(text) for text in texts if text.startswith('{"matched_position"')
                ]
                assert windows
                assert windows[0]["messages"][1]["text"] == "No, I bought the red one."
                assert windows[0]["messages"][1]["role"] == "user"
                assert any(text.startswith('{"matched_position"') for text in candidate_texts)
                for text in texts:
                    if text.startswith('{"matched_position"'):
                        assert text in candidate_texts
            else:
                assert "No, I bought the red one." in texts
                assert not any(text.startswith('{"matched_position"') for text in texts)
                assert not any(text.startswith('{"matched_position"') for text in candidate_texts)
    finally:
        assert DeletionService(repository, asset_store).delete_run(run, user_id=user).complete
