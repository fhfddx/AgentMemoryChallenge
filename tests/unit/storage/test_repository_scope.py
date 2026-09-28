"""MemoryRepository 用户作用域与多模态向量空间隔离单元测试。"""

import inspect
from uuid import uuid4

import pytest

from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository
from masm.storage.types import EmbeddingDraft, MemoryBundle, MemoryDraft

# 所有读取接口都必须强制要求 user_id。
READ_METHODS = ("get_by_request", "lexical_candidates", "vector_candidates", "related")


def _uid(prefix: str) -> str:
    """生成每次运行都唯一的用户标识，避免跨测试污染。"""
    return f"{prefix}-{uuid4().hex[:12]}"


def _bundle(
    user_id: str,
    text: str,
    *,
    vector: list[float] | None = None,
    modality: str = "text",
    model_name: str = "fake-embedding",
    model_version: str = "v1",
) -> MemoryBundle:
    """构造一条记忆束；给定 vector 时附带对应模态与模型的向量。"""
    embedding = None
    if vector is not None:
        embedding = EmbeddingDraft(
            modality=modality,
            model_name=model_name,
            model_version=model_version,
            dimensions=len(vector),
            vector=vector,
        )
    return MemoryBundle(
        session_id=f"session-{user_id}",
        request_id=f"request-{uuid4().hex}",
        memories=[
            MemoryDraft(
                summary=text,
                original_text=text,
                keywords=[],
                modality=modality,
                embedding=embedding,
            )
        ],
    )


@pytest.fixture(scope="module")
def database(database_url: str) -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(database_url)


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
    repo.add_bundle(user_a, _bundle(user_a, "the cat memory alpha"))
    repo.add_bundle(user_b, _bundle(user_b, "the cat memory beta"))

    results = repo.lexical_candidates(user_a, "cat", limit=10)
    assert results, "应能检索到 user_a 的记忆"
    assert all(c.user_id == user_a for c in results)
    assert any("alpha" in c.content for c in results)


def test_vector_candidates_scoped_to_user(database: Database) -> None:
    """向量候选只返回目标用户的记录。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    repo.add_bundle(user_a, _bundle(user_a, "alpha memory", vector=[1.0, 0.0, 0.0]))
    repo.add_bundle(user_b, _bundle(user_b, "beta memory", vector=[1.0, 0.0, 0.0]))

    results = repo.vector_candidates(
        user_a,
        [1.0, 0.0, 0.0],
        modality="text",
        model_name="fake-embedding",
        model_version="v1",
        limit=10,
    )
    assert results, "应能检索到 user_a 的记忆"
    assert all(c.user_id == user_a for c in results)
    assert any("alpha" in c.content for c in results)


def test_vector_candidates_mixed_dimensions_do_not_raise(database: Database) -> None:
    """同一用户存在不同维度向量时，查询不得抛出 different vector dimensions。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    repo.add_bundle(user, _bundle(user, "text memory", vector=[1.0, 0.0, 0.0], modality="text"))
    repo.add_bundle(
        user,
        _bundle(user, "image memory", vector=[1.0, 0.0, 0.0, 0.0], modality="image"),
    )

    results = repo.vector_candidates(
        user,
        [1.0, 0.0, 0.0],
        modality="text",
        model_name="fake-embedding",
        model_version="v1",
        limit=10,
    )
    assert [c.content for c in results] == ["text memory"]


def test_vector_candidates_isolated_by_modality(database: Database) -> None:
    """查询文本空间不能返回图片空间记录。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    repo.add_bundle(user, _bundle(user, "text record", vector=[1.0, 0.0, 0.0], modality="text"))
    repo.add_bundle(user, _bundle(user, "image record", vector=[1.0, 0.0, 0.0], modality="image"))

    results = repo.vector_candidates(
        user,
        [1.0, 0.0, 0.0],
        modality="text",
        model_name="fake-embedding",
        model_version="v1",
        limit=10,
    )
    assert results
    assert all(c.content == "text record" for c in results)


def test_vector_candidates_isolated_by_model(database: Database) -> None:
    """相同维度但模型名称或版本不同时不能混合检索。"""
    repo = MemoryRepository(database)
    user = _uid("user")
    repo.add_bundle(user, _bundle(user, "model a", vector=[1.0, 0.0, 0.0], model_name="model-a"))
    repo.add_bundle(user, _bundle(user, "model b", vector=[1.0, 0.0, 0.0], model_name="model-b"))
    repo.add_bundle(
        user,
        _bundle(
            user,
            "model a v2",
            vector=[1.0, 0.0, 0.0],
            model_name="model-a",
            model_version="v2",
        ),
    )

    results = repo.vector_candidates(
        user,
        [1.0, 0.0, 0.0],
        modality="text",
        model_name="model-a",
        model_version="v1",
        limit=10,
    )
    assert [c.content for c in results] == ["model a"]


def test_vector_candidates_ignores_other_users_embeddings(database: Database) -> None:
    """其他用户的 Embedding 不能影响目标用户结果。"""
    repo = MemoryRepository(database)
    user_a = _uid("user-a")
    user_b = _uid("user-b")
    repo.add_bundle(user_a, _bundle(user_a, "alpha", vector=[1.0, 0.0, 0.0]))
    repo.add_bundle(user_b, _bundle(user_b, "beta", vector=[1.0, 0.0, 0.0]))

    results = repo.vector_candidates(
        user_a,
        [1.0, 0.0, 0.0],
        modality="text",
        model_name="fake-embedding",
        model_version="v1",
        limit=10,
    )
    assert all(c.user_id == user_a for c in results)
    assert {c.content for c in results} == {"alpha"}
