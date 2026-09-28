"""初始记忆数据模型（设计规范 7.1 的 12 张必需表，固定快照）。

本迁移是自包含的固定快照，不依赖 models.py；使用明确的 op.create_table、
op.create_index 与约束。downgrade 按依赖逆序删除索引与表。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def _pk(column_name: str = "id") -> sa.Column:
    return sa.Column(
        column_name, UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _user_id() -> sa.Column:
    return sa.Column(
        "user_id",
        sa.String(255),
        sa.ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    """创建 pgvector 扩展、全部数据表与索引。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("user_id", sa.String(255), primary_key=True),
        _created_at(),
    )

    op.create_table(
        "sessions",
        _pk(),
        _user_id(),
        sa.Column("session_id", sa.String(255), nullable=False),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("user_id", "session_id", name="uq_sessions_user_session"),
    )

    op.create_table(
        "source_messages",
        _pk(),
        _user_id(),
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=True),
        _created_at(),
    )

    op.create_table(
        "assets",
        _pk(),
        _user_id(),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("decoded_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        _created_at(),
    )

    op.create_table(
        "memories",
        _pk(),
        _user_id(),
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("keywords", JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("modality", sa.String(32), nullable=False, server_default=sa.text("'text'")),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_precision", sa.String(32), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("duplicate_of", UUID, nullable=True),
        sa.Column("supersedes", UUID, nullable=True),
        sa.Column("conflict_group_id", UUID, nullable=True),
        _created_at(),
    )

    op.create_table(
        "memory_entities",
        _pk(),
        _user_id(),
        sa.Column(
            "memory_id",
            UUID,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False, server_default=sa.text("'object'")),
        sa.Column("normalized_label", sa.String(255), nullable=False, server_default=sa.text("''")),
        _created_at(),
        sa.UniqueConstraint("memory_id", "name", "entity_type", name="uq_memory_entities"),
    )

    op.create_table(
        "memory_events",
        _pk(),
        _user_id(),
        sa.Column(
            "memory_id",
            UUID,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_precision", sa.String(32), nullable=True),
        sa.Column("normalized_time", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        _created_at(),
    )

    op.create_table(
        "memory_relations",
        _pk(),
        _user_id(),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", UUID, nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_ref", JSONB, nullable=True),
        _created_at(),
    )

    op.create_table(
        "memory_embeddings",
        _pk(),
        _user_id(),
        sa.Column(
            "memory_id",
            UUID,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", Vector(), nullable=False),
        _created_at(),
    )

    op.create_table(
        "memory_conflicts",
        _pk(),
        _user_id(),
        sa.Column("conflict_group_id", UUID, nullable=False),
        sa.Column(
            "memory_id",
            UUID,
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _created_at(),
    )

    op.create_table(
        "processing_runs",
        _pk(),
        _user_id(),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("code_version", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("token_usage", JSONB, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'completed'")),
        sa.Column("result_metadata", JSONB, nullable=True),
        _created_at(),
    )

    op.create_table(
        "request_ledger",
        _pk(),
        _user_id(),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'PROCESSING'")),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("user_id", "request_id", name="uq_request_ledger_user_request"),
    )

    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_source_messages_user_id", "source_messages", ["user_id"])
    op.create_index("ix_source_messages_session_id", "source_messages", ["session_id"])
    op.create_index("ix_source_messages_request_id", "source_messages", ["request_id"])
    op.create_index("ix_assets_user_id", "assets", ["user_id"])
    op.create_index("ix_assets_content_hash", "assets", ["content_hash"])
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_index("ix_memories_session_id", "memories", ["session_id"])
    op.create_index("ix_memories_request_id", "memories", ["request_id"])
    op.create_index("ix_memories_conflict_group_id", "memories", ["conflict_group_id"])
    op.create_index("ix_memory_entities_user_id", "memory_entities", ["user_id"])
    op.create_index("ix_memory_entities_memory_id", "memory_entities", ["memory_id"])
    op.create_index("ix_memory_events_user_id", "memory_events", ["user_id"])
    op.create_index("ix_memory_events_memory_id", "memory_events", ["memory_id"])
    op.create_index("ix_memory_relations_user_id", "memory_relations", ["user_id"])
    op.create_index("ix_memory_relations_source_id", "memory_relations", ["source_id"])
    op.create_index("ix_memory_relations_target_id", "memory_relations", ["target_id"])
    op.create_index("ix_memory_embeddings_user_id", "memory_embeddings", ["user_id"])
    op.create_index("ix_memory_embeddings_memory_id", "memory_embeddings", ["memory_id"])
    op.create_index("ix_memory_conflicts_user_id", "memory_conflicts", ["user_id"])
    op.create_index(
        "ix_memory_conflicts_conflict_group_id", "memory_conflicts", ["conflict_group_id"]
    )
    op.create_index("ix_memory_conflicts_memory_id", "memory_conflicts", ["memory_id"])
    op.create_index("ix_processing_runs_user_id", "processing_runs", ["user_id"])
    op.create_index("ix_processing_runs_request_id", "processing_runs", ["request_id"])
    op.create_index("ix_request_ledger_user_id", "request_ledger", ["user_id"])


def downgrade() -> None:
    """按依赖逆序删除索引、表与扩展。"""
    op.drop_index("ix_request_ledger_user_id", table_name="request_ledger")
    op.drop_index("ix_processing_runs_request_id", table_name="processing_runs")
    op.drop_index("ix_processing_runs_user_id", table_name="processing_runs")
    op.drop_index("ix_memory_conflicts_memory_id", table_name="memory_conflicts")
    op.drop_index("ix_memory_conflicts_conflict_group_id", table_name="memory_conflicts")
    op.drop_index("ix_memory_conflicts_user_id", table_name="memory_conflicts")
    op.drop_index("ix_memory_embeddings_memory_id", table_name="memory_embeddings")
    op.drop_index("ix_memory_embeddings_user_id", table_name="memory_embeddings")
    op.drop_index("ix_memory_relations_target_id", table_name="memory_relations")
    op.drop_index("ix_memory_relations_source_id", table_name="memory_relations")
    op.drop_index("ix_memory_relations_user_id", table_name="memory_relations")
    op.drop_index("ix_memory_events_memory_id", table_name="memory_events")
    op.drop_index("ix_memory_events_user_id", table_name="memory_events")
    op.drop_index("ix_memory_entities_memory_id", table_name="memory_entities")
    op.drop_index("ix_memory_entities_user_id", table_name="memory_entities")
    op.drop_index("ix_memories_conflict_group_id", table_name="memories")
    op.drop_index("ix_memories_request_id", table_name="memories")
    op.drop_index("ix_memories_session_id", table_name="memories")
    op.drop_index("ix_memories_user_id", table_name="memories")
    op.drop_index("ix_assets_content_hash", table_name="assets")
    op.drop_index("ix_assets_user_id", table_name="assets")
    op.drop_index("ix_source_messages_request_id", table_name="source_messages")
    op.drop_index("ix_source_messages_session_id", table_name="source_messages")
    op.drop_index("ix_source_messages_user_id", table_name="source_messages")
    op.drop_index("ix_sessions_user_id", table_name="sessions")

    op.drop_table("memory_conflicts")
    op.drop_table("memory_embeddings")
    op.drop_table("memory_relations")
    op.drop_table("memory_events")
    op.drop_table("memory_entities")
    op.drop_table("memories")
    op.drop_table("processing_runs")
    op.drop_table("request_ledger")
    op.drop_table("source_messages")
    op.drop_table("assets")
    op.drop_table("sessions")
    op.drop_table("users")

    op.execute("DROP EXTENSION IF EXISTS vector")
