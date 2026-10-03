"""数据库迁移集成测试。"""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from conftest import require_masm_test_database
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository

REPO_ROOT = Path(__file__).resolve().parents[3]

# 设计规范 7.1 要求的全部必需数据表。
REQUIRED_TABLES = {
    "users",
    "sessions",
    "source_messages",
    "assets",
    "memories",
    "memory_entities",
    "memory_events",
    "memory_relations",
    "memory_embeddings",
    "memory_conflicts",
    "processing_runs",
    "request_ledger",
}


def _alembic_config(url: str) -> Config:
    """构造指向仓库 alembic 目录与目标数据库的配置。"""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def database(database_url: str) -> Database:
    """连接已迁移的测试数据库。"""
    return Database.create(database_url)


def test_migration_upgrade_downgrade_cycle(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从空库 downgrade、upgrade 的完整循环，且只作用于已验证的 masm_test。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    # 模拟外部 DATABASE_URL 指向其他库（危险场景）。
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5433/production")
    # 用已验证的 masm_test URL 覆盖，确保 alembic/env.py 实际使用的是安全库。
    with patch.dict(os.environ, {"DATABASE_URL": validated}):
        command.downgrade(cfg, "base")
        remaining = set(inspect(engine).get_table_names())
        assert not (REQUIRED_TABLES & remaining), (
            f"downgrade 后业务表仍存在: {REQUIRED_TABLES & remaining}"
        )

        command.upgrade(cfg, "head")

    tables = set(inspect(engine).get_table_names())
    missing = REQUIRED_TABLES - tables
    assert not missing, f"upgrade head 后缺少表: {sorted(missing)}"

    with engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0004"

    # 0002 引入的冲突组唯一约束必须存在。
    constraint_names = {
        constraint["name"]
        for constraint in inspect(engine).get_unique_constraints("memory_conflicts")
    }
    assert {
        "uq_memory_conflicts_group_memory",
        "uq_memory_conflicts_group_version",
    } <= constraint_names


def test_all_required_tables_exist(database: Database) -> None:
    """迁移后所有必需数据表都存在。"""
    existing = set(inspect(database.engine).get_table_names())
    missing = REQUIRED_TABLES - existing
    assert not missing, f"缺少数据表: {sorted(missing)}"


def test_0004_backfills_legacy_context(database_url: str) -> None:
    """旧记忆在 0003 → 0004 后仍可按原用户读取，唯一约束不跨用户。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)
    user_a = f"legacy-a-{uuid4().hex}"
    user_b = f"legacy-b-{uuid4().hex}"
    request_id = f"same-run-{uuid4().hex}"

    with patch.dict(os.environ, {"DATABASE_URL": validated}):
        # 测试库可被其他用例写入消息记忆；先清空再建立真正的 0003 旧库快照。
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "0003")
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO users (user_id) VALUES (:a), (:b)"),
                {"a": user_a, "b": user_b},
            )
            conn.execute(
                sa.text("INSERT INTO sessions (user_id, session_id) VALUES (:u, 'session-1')"),
                {"u": user_a},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO memories (user_id, session_id, request_id, summary) "
                    "SELECT CAST(:u AS varchar), id, CAST(:r AS varchar), 'old evidence' "
                    "FROM sessions WHERE user_id = CAST(:u AS varchar)"
                ),
                {"u": user_a, "r": request_id},
            )
        command.upgrade(cfg, "head")

    with engine.begin() as conn:
        legacy = conn.execute(
            sa.text(
                "SELECT granularity, source_position FROM memories "
                "WHERE user_id = CAST(:u AS varchar) AND request_id = CAST(:r AS varchar)"
            ),
            {"u": user_a, "r": request_id},
        ).one()
        assert tuple(legacy) == ("context", None)
        conn.execute(
            sa.text("INSERT INTO sessions (user_id, session_id) VALUES (:u, 'session-1')"),
            {"u": user_b},
        )
        conn.execute(
            sa.text(
                "INSERT INTO memories (user_id, session_id, request_id, summary) "
                "SELECT CAST(:u AS varchar), id, CAST(:r AS varchar), 'other evidence' "
                "FROM sessions WHERE user_id = CAST(:u AS varchar)"
            ),
            {"u": user_b, "r": request_id},
        )
        count = conn.execute(
            sa.text("SELECT count(*) FROM memories WHERE request_id = CAST(:r AS varchar)"),
            {"r": request_id},
        ).scalar_one()
        assert count == 2


def test_pgvector_extension_and_vector_column(database: Database) -> None:
    """pgvector 扩展和 vector 列存在。"""
    with database.engine.connect() as conn:
        ext = conn.execute(
            sa.text("SELECT extname FROM pg_extension WHERE extname='vector'")
        ).scalar()
    assert ext == "vector"

    columns = {col["name"] for col in inspect(database.engine).get_columns("memory_embeddings")}
    assert "vector" in columns


def test_embeddings_table_records_model_metadata(database: Database) -> None:
    """向量记录包含模型名、版本和维度。"""
    columns = {col["name"] for col in inspect(database.engine).get_columns("memory_embeddings")}
    assert {"model_name", "model_version", "dimensions", "vector"} <= columns


def test_every_retrievable_table_has_user_id(database: Database) -> None:
    """每条可检索记录都必须直接包含 user_id 列。"""
    inspector = inspect(database.engine)
    for table in ("memories", "memory_entities", "memory_events", "memory_relations"):
        columns = {col["name"] for col in inspector.get_columns(table)}
        assert "user_id" in columns, f"{table} 缺少 user_id 列"


def _seed_legacy_0002_data(engine: sa.Engine) -> None:
    """在 0002 结构上写入历史数据：资产行没有 request_id，只有原始消息能反查归属。

    覆盖四种情形：
    - ``ns-a/shared.png``：同一用户的两次运行都引用 -> 取字典序最小的 request_id；
    - ``ns-a/orphan.png``：没有任何消息引用 -> 保持 NULL；
    - ``ns-b/only.png``：唯一引用 -> 回填到该运行；
    - ``ns-c/aliased.png``：user_b 的消息引用了 user_c 的资产地址 -> 绝不跨用户回填。
    """
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (user_id) VALUES ('legacy-a'), ('legacy-b')"))
        conn.execute(
            sa.text(
                "INSERT INTO sessions (user_id, session_id) VALUES "
                "('legacy-a', 's-a'), ('legacy-b', 's-b')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO source_messages "
                "(user_id, session_id, request_id, position, role, content) "
                "SELECT CAST(:user_id AS varchar), id, CAST(:request_id AS varchar), 0, "
                "'user', CAST(:content AS jsonb) "
                "FROM sessions WHERE user_id = CAST(:user_id AS varchar) "
                "AND session_id = CAST(:session_id AS varchar)"
            ),
            [
                {
                    "user_id": "legacy-a",
                    "session_id": "s-a",
                    "request_id": "run-z",
                    "content": '[{"type": "image_url", "image_url": {"url": "ns-a/shared.png"}}]',
                },
                {
                    "user_id": "legacy-a",
                    "session_id": "s-a",
                    "request_id": "run-a",
                    "content": '[{"type": "image_url", "image_url": {"url": "ns-a/shared.png"}}]',
                },
                {
                    "user_id": "legacy-b",
                    "session_id": "s-b",
                    "request_id": "run-b",
                    "content": '[{"type": "image_url", "image_url": {"url": "ns-b/only.png"}}]',
                },
                {
                    "user_id": "legacy-b",
                    "session_id": "s-b",
                    "request_id": "run-b",
                    "content": '[{"type": "text", "text": "no image here"}]',
                },
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO assets "
                "(user_id, object_uri, media_type, content_hash, decoded_size) VALUES "
                "('legacy-a', 'ns-a/shared.png', 'image/png', 'h1', 1), "
                "('legacy-a', 'ns-a/orphan.png', 'image/png', 'h2', 1), "
                "('legacy-b', 'ns-b/only.png', 'image/png', 'h3', 1), "
                "('legacy-a', 'ns-b/only.png', 'image/png', 'h4', 1)"
            )
        )


def _cleanup_legacy(engine: sa.Engine) -> None:
    """清理本次迁移测试写入的历史数据（head 结构，级联删除即可）。"""
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE user_id LIKE 'legacy-%'"))


def test_0003_backfills_legacy_asset_request_ids(database_url: str) -> None:
    """真实 0002 -> head 数据迁移必须回填历史资产的 request_id，且不跨用户串味。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    command.downgrade(cfg, "0002")
    with engine.connect() as conn:
        version_before = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert version_before == "0002"
    _seed_legacy_0002_data(engine)

    with engine.connect() as conn:
        columns_before = {col["name"] for col in inspect(engine).get_columns("assets")}
    assert "request_id" not in columns_before

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        mapped = {
            (row.user_id, row.object_uri): row.request_id
            for row in conn.execute(sa.text("SELECT user_id, object_uri, request_id FROM assets"))
        }

    assert mapped[("legacy-a", "ns-a/shared.png")] == "run-a"
    assert mapped[("legacy-a", "ns-a/orphan.png")] is None
    assert mapped[("legacy-b", "ns-b/only.png")] == "run-b"
    # 同一个对象地址在不同用户下是不同资产，绝不能被另一位用户的运行"认领"。
    assert mapped[("legacy-a", "ns-b/only.png")] is None

    _cleanup_legacy(engine)
    engine.dispose()


def test_0003_backfill_is_repeatable_and_lossless_on_reupgrade(database_url: str) -> None:
    """回填结果在 0002 -> head 循环后仍然确定：重新迁移不会改变归属，也不会重复回填。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    command.downgrade(cfg, "0002")
    _seed_legacy_0002_data(engine)
    command.upgrade(cfg, "head")

    def _snapshot() -> dict:
        with engine.connect() as conn:
            return {
                (row.user_id, row.object_uri): row.request_id
                for row in conn.execute(
                    sa.text("SELECT user_id, object_uri, request_id FROM assets")
                )
            }

    first = _snapshot()

    # 回到 0002（丢列）再升到 head：等价于历史库再次迁移。
    command.downgrade(cfg, "0002")
    command.upgrade(cfg, "head")

    assert _snapshot() == first
    assert first[("legacy-a", "ns-a/shared.png")] == "run-a"

    _cleanup_legacy(engine)
    engine.dispose()


def test_0003_shared_legacy_object_survives_until_the_last_run_is_deleted(
    database_url: str, asset_store: AssetStore, settings, embeddings
) -> None:
    """真实删除验证：迁移前两个运行共享同一对象，删除第一个后对象必须仍在。

    旧回填把同一 (user_id, object_uri) 统一归给 MIN(request_id)，只信资产行时会把
    「仍被第二个运行引用」的对象误判为独占；因此这里直接跑真实删除路径，断言：
    删除 run-a 后对象仍存在（第二个运行还在引用），删除 run-z 后对象才消失。
    """
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    command.downgrade(cfg, "0002")
    _seed_legacy_0002_data(engine)
    command.upgrade(cfg, "head")

    # 在真实对象存储里创建该共享对象，使物理删除可观察。
    shared_uri, orphan_uri = "ns-a/shared.png", "ns-a/orphan.png"
    target = asset_store.base_dir / shared_uri
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"shared-bytes")
    assert target.exists()

    database = Database.create(validated)
    repo = MemoryRepository(database)
    service = DeletionService(repo, asset_store)

    # run-a（字典序最小、被回填认领）先删除：run-z 的原始消息仍引用同一对象。
    first = service.delete_run("run-a", user_id="legacy-a")

    assert target.exists(), "删除第一个运行后，第二个运行仍引用的对象不得被删除"
    assert first.objects_deleted == 0

    second = service.delete_run("run-z", user_id="legacy-a")

    assert second.complete is True
    assert not target.exists(), "最后一个引用者被删除后，对象才允许消失"

    # 无任何消息引用的遗留资产（orphan）不应被任何运行删除。
    assert orphan_uri not in second.failed_object_uris

    _cleanup_legacy(engine)
    database.engine.dispose()
    engine.dispose()


def test_0003_downgrade_removes_local_state(database_url: str) -> None:
    """0003 的 downgrade 必须干净移除本迁移新增的列与表，并保持可再次 upgrade。"""
    validated = require_masm_test_database(database_url)
    cfg = _alembic_config(validated)
    engine = create_engine(validated)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")

    inspector = inspect(engine)
    assert "deletion_intents" not in set(inspector.get_table_names())
    assert "request_id" not in {col["name"] for col in inspector.get_columns("assets")}

    command.upgrade(cfg, "head")
    engine.dispose()
