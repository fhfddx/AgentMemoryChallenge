"""MemoryRepository 用户作用域单元测试。"""

import inspect
from uuid import uuid4

import pytest

from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository
from masm.storage.types import EmbeddingDraft, MemoryBundle, MemoryDraft

# 测试数据库连接串，可用 MASM_TEST_DATABASE_URL 覆盖。
TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5433/masm_test"

# 所有读取接口都必须强制要求 user_id。
READ_METHODS = ("get_by_request", "lexical_candidates", "vector_candidates", "related")


def _uid(prefix: str) -> str:
    """生成每次运行都唯一的用户标识，避免跨测试污染。"""
    return f"{prefix}-{uuid4().hex[:12]}"


def _bundle(user_id: str, text: str, vector: list[float]) -> MemoryBundle:
    """构造一条带文本向量、可被全文和向量检索的最小记忆束。"""
    return MemoryBundle(
        session_id=f"session-{user_id}",
        request_id=f"request-{user_id}",
        memories=[
            MemoryDraft(
                summary=text,
                original_text=text,
                keywords=[],
                modality="text",
                embedding=EmbeddingDraft(
                    modality="text",
                    model_name="fake-embedding",
                    model_version="v1",
                    dimensions=len(vector),
                    vector=vector,
                ),
            )
        ],
    )


@pytest.fixture(scope="module")
def database() -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(TEST_DATABASE_URL)


def test_read_methods_require_user_id() -> None:
    """所有读取接口都必须强制要求 user_id，且不能有默认值。"""
    for name in READ_METHODS:
        method = getattr(MemoryRepository, name)
        params = inspect.signature(method).parameters
        assert "user_id" in params, f"{name} 缺少 user_id 参数"
        assert params["user_id"].default is inspect.Parameter.empty, (
            f"{name} 的 user_id 不能有默认值"
        )


def test_lexical_candidates_scoped_to_user(database: Database) -> None:
    """全文候选只返回目标用户的记录。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    repo.add_bundle(user_a, _bundle(user_a, "the cat memory alpha", [1.0, 0.0, 0.0]))
    repo.add_bundle(user_b, _bundle(user_b, "the cat memory beta", [1.0, 0.0, 0.0]))

    results = repo.lexical_candidates(user_a, "cat", limit=10)
    assert results, "应能检索到 user_a 的记忆"
    assert all(c.user_id == user_a for c in results)
    assert any("alpha" in c.content for c in results)


def test_vector_candidates_scoped_to_user(database: Database) -> None:
    """向量候选只返回目标用户的记录。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    repo.add_bundle(user_a, _bundle(user_a, "alpha memory", [1.0, 0.0, 0.0]))
    repo.add_bundle(user_b, _bundle(user_b, "beta memory", [1.0, 0.0, 0.0]))

    results = repo.vector_candidates(user_a, [1.0, 0.0, 0.0], limit=10)
    assert results, "应能检索到 user_a 的记忆"
    assert all(c.user_id == user_a for c in results)
    assert any("alpha" in c.content for c in results)
