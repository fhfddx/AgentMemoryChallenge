"""应用重建后的 HTTP 持久性测试。"""

from uuid import uuid4

from fastapi.testclient import TestClient

from masm.api.app import create_app


def test_add_is_searchable_after_application_restart(
    database, asset_store, settings, embeddings
) -> None:
    """关闭并重建应用实例后，原 Add 数据仍能通过公共 Search 找到。"""
    marker = uuid4().hex
    user_id = f"restart-user-{marker}"
    request_id = f"restart-request-{marker}"
    headers = {"X-Api-Key": "test-key"}
    add_payload = {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": f"restart-session-{marker}",
        "messages": [{"role": "user", "content": f"durable restart marker {marker}"}],
    }

    with TestClient(
        create_app(
            settings,
            database=database,
            asset_store=asset_store,
            embeddings=embeddings,
        )
    ) as first:
        added = first.post("/add", headers=headers, json=add_payload)
        assert added.status_code == 200

    with TestClient(
        create_app(
            settings,
            database=database,
            asset_store=asset_store,
            embeddings=embeddings,
        )
    ) as restarted:
        found = restarted.post(
            "/search",
            headers=headers,
            json={"query": marker, "user_id": user_id, "top_k": 10},
        )

    assert found.status_code == 200
    assert any(marker in str(item["content"]) for item in found.json()["data"])
