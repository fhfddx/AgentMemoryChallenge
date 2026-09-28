"""SQLAlchemy 数据模型（设计规范 7.1 的必需数据表）。

原始内容（source_messages、assets）与派生内容（memories、memory_entities 等）分表存储；
每条可检索记录都直接包含 user_id，向量记录包含模型名、版本和维度。
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _user_fk() -> Mapped[str]:
    return mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class User(Base):
    """匿名比赛用户标识。"""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = _created_at()


class SessionRecord(Base):
    """会话及时间戳。"""

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("user_id", "session_id", name="uq_sessions_user_session"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SourceMessage(Base):
    """不可变的原始消息和有序内容分片。"""

    __tablename__ = "source_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[Any] = mapped_column(JSONB, nullable=False)
    timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Asset(Base):
    """对象地址、媒体类型、哈希、解码后大小和尺寸。

    request_id 记录该资产属于哪一次运行，是运行级删除的唯一依据。
    """

    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decoded_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Memory(Base):
    """结构化记忆单元（派生内容）。"""

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    modality: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_precision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime] = _created_at()
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    supersedes: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    conflict_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    created_at: Mapped[datetime] = _created_at()


class MemoryEntity(Base):
    """人物、地点、物体和规范化标签。"""

    __tablename__ = "memory_entities"
    __table_args__ = (
        UniqueConstraint("memory_id", "name", "entity_type", name="uq_memory_entities"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="object")
    normalized_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = _created_at()


class MemoryEvent(Base):
    """事件及归一化时间属性。"""

    __tablename__ = "memory_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_precision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalized_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created_at()


class MemoryRelation(Base):
    """记忆、实体和事件之间的有向类型边（两端必须属于同一用户）。"""

    __tablename__ = "memory_relations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class MemoryEmbedding(Base):
    """带模型版本的文本和图片向量。"""

    __tablename__ = "memory_embeddings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    modality: Mapped[str] = mapped_column(String(16), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class MemoryConflict(Base):
    """冲突组和版本关系。"""

    __tablename__ = "memory_conflicts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "conflict_group_id", "memory_id", name="uq_memory_conflicts_group_memory"
        ),
        UniqueConstraint(
            "user_id", "conflict_group_id", "version", name="uq_memory_conflicts_group_version"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    conflict_group_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()


class DeletionIntent(Base):
    """持久化的对象删除意图：保证删除可重试、可跨进程恢复。

    status 为 PENDING 表示对象尚未确认删除（或不存在）；DONE 表示已删除或确定不存在。
    """

    __tablename__ = "deletion_intents"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "request_id", "object_uri", name="uq_deletion_intents_scope_uri"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcessingRun(Base):
    """模型、Prompt、代码版本、延迟和处理结果元数据。"""

    __tablename__ = "processing_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    result_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class RequestLedger(Base):
    """Add 请求的幂等状态。"""

    __tablename__ = "request_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_request_ledger_user_request"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = _user_fk()
    request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PROCESSING")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
