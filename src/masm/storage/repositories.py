"""带强制用户作用域的 MemoryRepository。

所有读取接口都必须显式接收 user_id，并在 SQL 查询阶段按 user_id 过滤，
绝不依赖查询后的 Python 过滤。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from masm.storage.db import Database
from masm.storage.models import (
    Memory,
    MemoryEmbedding,
    MemoryRelation,
    RequestLedger,
    SessionRecord,
    SourceMessage,
    User,
)
from masm.storage.types import AddCommit, MemoryBundle, MemoryCandidate


class MemoryRepository:
    """记忆存储仓库。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def add_bundle(self, user_id: str, bundle: MemoryBundle) -> AddCommit:
        """在同一事务单元中持久化一个记忆束。"""
        with self._database.session() as session:
            self._ensure_user(session, user_id)
            session_model = self._ensure_session(session, user_id, bundle.session_id)
            session.add(
                RequestLedger(
                    user_id=user_id,
                    request_id=bundle.request_id,
                    session_id=bundle.session_id,
                    status="COMMITTED",
                )
            )
            for message in bundle.messages:
                session.add(
                    SourceMessage(
                        user_id=user_id,
                        session_id=session_model.id,
                        request_id=bundle.request_id,
                        position=message.position,
                        role=message.role,
                        content=message.content,
                        timestamp=message.timestamp,
                    )
                )
            memory_ids: list[UUID] = []
            for draft in bundle.memories:
                memory = Memory(
                    user_id=user_id,
                    session_id=session_model.id,
                    request_id=bundle.request_id,
                    summary=draft.summary,
                    original_text=draft.original_text,
                    keywords=list(draft.keywords),
                    modality=draft.modality,
                    event_time=draft.event_time,
                    time_precision=draft.time_precision,
                    confidence=draft.confidence,
                    status="active",
                )
                session.add(memory)
                session.flush()
                memory_ids.append(memory.id)
                if draft.embedding is not None:
                    session.add(
                        MemoryEmbedding(
                            user_id=user_id,
                            memory_id=memory.id,
                            modality=draft.embedding.modality,
                            model_name=draft.embedding.model_name,
                            model_version=draft.embedding.model_version,
                            dimensions=draft.embedding.dimensions,
                            vector=list(draft.embedding.vector),
                        )
                    )
            session.commit()
        return AddCommit(
            request_id=bundle.request_id,
            user_id=user_id,
            session_id=bundle.session_id,
            memory_ids=memory_ids,
        )

    def get_by_request(self, user_id: str, request_id: str) -> AddCommit | None:
        """按 user_id + request_id 返回已提交结果；不存在时返回 None。"""
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger).where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
            ).scalar_one_or_none()
            if ledger is None:
                return None
            memory_ids = list(
                session.execute(
                    select(Memory.id).where(
                        Memory.user_id == user_id,
                        Memory.request_id == request_id,
                    )
                ).scalars()
            )
        return AddCommit(
            request_id=ledger.request_id,
            user_id=ledger.user_id,
            session_id=ledger.session_id,
            memory_ids=memory_ids,
        )

    def lexical_candidates(self, user_id: str, query: str, limit: int) -> list[MemoryCandidate]:
        """全文检索，SQL 查询阶段即按 user_id 过滤。"""
        with self._database.session() as session:
            ts_vector = func.to_tsvector("simple", Memory.summary)
            ts_query = func.plainto_tsquery("simple", query)
            rank = func.ts_rank(ts_vector, ts_query)
            stmt = (
                select(Memory, rank.label("rank"))
                .where(Memory.user_id == user_id, ts_vector.op("@@")(ts_query))
                .order_by(rank.desc())
                .limit(limit)
            )
            rows = session.execute(stmt).all()
        return [
            MemoryCandidate(
                memory_id=row.Memory.id,
                user_id=row.Memory.user_id,
                content=row.Memory.summary,
                score=float(row.rank),
            )
            for row in rows
        ]

    def vector_candidates(
        self, user_id: str, vector: Sequence[float], limit: int
    ) -> list[MemoryCandidate]:
        """向量检索，SQL 查询阶段即按 user_id 过滤。"""
        query_vector = list(vector)
        with self._database.session() as session:
            distance = MemoryEmbedding.vector.cosine_distance(query_vector)
            stmt = (
                select(Memory, distance.label("distance"))
                .join(MemoryEmbedding, MemoryEmbedding.memory_id == Memory.id)
                .where(Memory.user_id == user_id)
                .order_by(distance)
                .limit(limit)
            )
            rows = session.execute(stmt).all()
        return [
            MemoryCandidate(
                memory_id=row.Memory.id,
                user_id=row.Memory.user_id,
                content=row.Memory.summary,
                score=1.0 - float(row.distance),
            )
            for row in rows
        ]

    def related(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        """一跳关系扩展，SQL 查询阶段即按 user_id 过滤。"""
        ids = list(memory_ids)
        if not ids:
            return []
        with self._database.session() as session:
            relations = (
                session.execute(
                    select(MemoryRelation).where(
                        MemoryRelation.user_id == user_id,
                        or_(
                            MemoryRelation.source_id.in_(ids),
                            MemoryRelation.target_id.in_(ids),
                        ),
                    )
                )
                .scalars()
                .all()
            )
            related_ids: set[UUID] = set()
            for relation in relations:
                if relation.source_id in ids:
                    related_ids.add(relation.target_id)
                elif relation.target_id in ids:
                    related_ids.add(relation.source_id)
            if not related_ids:
                return []
            memories = (
                session.execute(
                    select(Memory)
                    .where(Memory.user_id == user_id, Memory.id.in_(list(related_ids)))
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return [
            MemoryCandidate(
                memory_id=memory.id,
                user_id=memory.user_id,
                content=memory.summary,
                score=0.0,
            )
            for memory in memories
        ]

    def _ensure_user(self, session: Session, user_id: str) -> None:
        """按需创建用户（幂等）。"""
        if session.get(User, user_id) is None:
            session.add(User(user_id=user_id))

    def _ensure_session(self, session: Session, user_id: str, session_id: str) -> SessionRecord:
        """按需创建会话（幂等）。"""
        existing = session.execute(
            select(SessionRecord).where(
                SessionRecord.user_id == user_id,
                SessionRecord.session_id == session_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        model = SessionRecord(user_id=user_id, session_id=session_id)
        session.add(model)
        session.flush()
        return model
