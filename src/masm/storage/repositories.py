"""带强制用户作用域的 MemoryRepository。

所有读取接口都必须显式接收 user_id，并在 SQL 查询阶段按 user_id 过滤，
绝不依赖查询后的 Python 过滤。关系写入前必须校验两端节点存在且属于同一用户。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from masm.storage.db import Database
from masm.storage.models import (
    Asset,
    Memory,
    MemoryEmbedding,
    MemoryEntity,
    MemoryEvent,
    MemoryRelation,
    RequestLedger,
    SessionRecord,
    SourceMessage,
    User,
)
from masm.storage.types import AddCommit, LedgerState, MemoryBundle, MemoryCandidate

# 关系边允许的节点类型。
_ALLOWED_NODE_TYPES = {"memory", "entity", "event"}

# 节点类型到 ORM 模型的映射。
_NODE_TABLES: dict[str, type[Memory] | type[MemoryEntity] | type[MemoryEvent]] = {
    "memory": Memory,
    "entity": MemoryEntity,
    "event": MemoryEvent,
}


class RelationValidationError(ValueError):
    """关系边校验失败。"""


class LedgerStateError(RuntimeError):
    """幂等账本状态不符合预期（例如所有权已被接管）。"""


class MemoryRepository:
    """记忆存储仓库。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def add_bundle(self, user_id: str, bundle: MemoryBundle) -> AddCommit:
        """在同一事务单元中持久化一个记忆束（不含幂等账本，供测试夹具与低层调用）。"""
        with self._database.session() as session:
            memory_ids = self._write_bundle(session, user_id, bundle)
            session.commit()
        return AddCommit(
            request_id=bundle.request_id,
            user_id=user_id,
            session_id=bundle.session_id,
            memory_ids=memory_ids,
        )

    def finalize_request(
        self, user_id: str, request_id: str, bundle: MemoryBundle
    ) -> AddCommit:
        """在单个数据库事务中完成最终提交。

        事务边界：锁定 RequestLedger -> 校验 PROCESSING -> 写入
        Session/Asset/SourceMessage/Memory -> 账本置 COMMITTED -> 一次性 commit。
        """
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger)
                .where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if ledger is None:
                raise LedgerStateError("幂等账本不存在，无法完成提交")
            if ledger.status != "PROCESSING":
                raise LedgerStateError(f"幂等账本状态不是 PROCESSING: {ledger.status}")
            memory_ids = self._write_bundle(session, user_id, bundle)
            ledger.status = "COMMITTED"
            session.commit()
        return AddCommit(
            request_id=bundle.request_id,
            user_id=user_id,
            session_id=bundle.session_id,
            memory_ids=memory_ids,
        )

    def _write_bundle(self, session: Session, user_id: str, bundle: MemoryBundle) -> list[UUID]:
        """在当前会话（事务）内写入 Session/Asset/SourceMessage/Memory。"""
        self._ensure_user(session, user_id)
        session_model = self._ensure_session(session, user_id, bundle.session_id)
        for asset in bundle.assets:
            session.add(
                Asset(
                    user_id=user_id,
                    object_uri=asset.object_uri,
                    media_type=asset.media_type,
                    content_hash=asset.content_hash,
                    decoded_size=asset.decoded_size,
                    width=asset.width,
                    height=asset.height,
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
        return memory_ids

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

    def get_ledger_status(self, user_id: str, request_id: str) -> str | None:
        """返回幂等账本状态（PROCESSING/COMMITTED/FAILED），不存在时返回 None。"""
        state = self.get_ledger(user_id, request_id)
        return state.status if state is not None else None

    def get_ledger(self, user_id: str, request_id: str) -> LedgerState | None:
        """返回幂等账本状态与租约时间戳。"""
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger).where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
            ).scalar_one_or_none()
        if ledger is None:
            return None
        return LedgerState(status=ledger.status, updated_at=ledger.updated_at)

    def claim_request(
        self, user_id: str, request_id: str, session_id: str, *, now: datetime
    ) -> bool:
        """原子地写入 PROCESSING 账本；返回 True 表示成功取得所有权。"""
        with self._database.session() as session:
            self._ensure_user(session, user_id)
            # 先落库 User，避免 RequestLedger 外键在未排序的 flush 中失败。
            session.flush()
            session.add(
                RequestLedger(
                    user_id=user_id,
                    request_id=request_id,
                    session_id=session_id,
                    status="PROCESSING",
                    updated_at=now,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    def takeover_request(
        self,
        user_id: str,
        request_id: str,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> bool:
        """原子接管已过期的 PROCESSING 租约；成功返回 True。"""
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger)
                .where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if ledger is None or ledger.status != "PROCESSING":
                return False
            if ledger.updated_at >= stale_before:
                return False
            ledger.updated_at = now
            session.commit()
            return True

    def mark_request(
        self,
        user_id: str,
        request_id: str,
        status: str,
        *,
        now: datetime,
        expect: str | None = None,
    ) -> bool:
        """更新幂等账本状态；expect 指定时仅当当前状态匹配才更新。"""
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger)
                .where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if ledger is None:
                return False
            if expect is not None and ledger.status != expect:
                return False
            ledger.status = status
            ledger.updated_at = now
            session.commit()
            return True

    def has_committed_data(self, user_id: str, request_id: str) -> bool:
        """该 request_id 是否已经有已提交的记忆数据。"""
        with self._database.session() as session:
            count = session.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.user_id == user_id, Memory.request_id == request_id)
            ).scalar_one()
        return bool(count)

    def count_asset_references(self, user_id: str, object_uri: str) -> int:
        """统计引用某对象键的已提交 Asset 记录数。"""
        with self._database.session() as session:
            count = session.execute(
                select(func.count())
                .select_from(Asset)
                .where(Asset.user_id == user_id, Asset.object_uri == object_uri)
            ).scalar_one()
        return int(count)

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
        self,
        user_id: str,
        vector: Sequence[float],
        *,
        modality: str,
        model_name: str,
        model_version: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        """向量检索：调用方必须明确目标向量空间（模态 + 模型 + 维度）。"""
        query_vector = list(vector)
        dimensions = len(query_vector)
        with self._database.session() as session:
            distance = MemoryEmbedding.vector.cosine_distance(query_vector)
            stmt = (
                select(Memory, distance.label("distance"))
                .join(MemoryEmbedding, MemoryEmbedding.memory_id == Memory.id)
                .where(
                    Memory.user_id == user_id,
                    MemoryEmbedding.user_id == user_id,
                    MemoryEmbedding.modality == modality,
                    MemoryEmbedding.model_name == model_name,
                    MemoryEmbedding.model_version == model_version,
                    MemoryEmbedding.dimensions == dimensions,
                )
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
        """一跳关系扩展（仅记忆到记忆），SQL 查询阶段即按 user_id 过滤。"""
        ids = list(memory_ids)
        if not ids:
            return []
        with self._database.session() as session:
            relations = (
                session.execute(
                    select(MemoryRelation).where(
                        MemoryRelation.user_id == user_id,
                        MemoryRelation.source_type == "memory",
                        MemoryRelation.target_type == "memory",
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

    def add_relation(
        self,
        user_id: str,
        *,
        source_type: str,
        source_id: UUID,
        target_type: str,
        target_id: UUID,
        relation_type: str,
        confidence: float | None = None,
        evidence_ref: dict | None = None,
    ) -> UUID:
        """写入一条关系边；写入前校验两端节点存在且属于同一用户。"""
        if source_type not in _ALLOWED_NODE_TYPES:
            raise RelationValidationError(f"不允许的 source_type: {source_type}")
        if target_type not in _ALLOWED_NODE_TYPES:
            raise RelationValidationError(f"不允许的 target_type: {target_type}")

        with self._database.session() as session:
            self._validate_node(session, source_type, source_id, user_id)
            self._validate_node(session, target_type, target_id, user_id)
            relation = MemoryRelation(
                user_id=user_id,
                source_type=source_type,
                source_id=source_id,
                target_type=target_type,
                target_id=target_id,
                relation_type=relation_type,
                confidence=confidence,
                evidence_ref=evidence_ref,
            )
            session.add(relation)
            session.flush()
            relation_id = relation.id
            session.commit()
        return relation_id

    def _validate_node(self, session: Session, node_type: str, node_id: UUID, user_id: str) -> None:
        """校验节点存在且属于指定用户。"""
        model: Any = _NODE_TABLES[node_type]
        owner: str | None = session.execute(
            select(model.user_id).where(model.id == node_id)
        ).scalar_one_or_none()
        if owner is None:
            raise RelationValidationError(f"{node_type} 节点不存在: {node_id}")
        if owner != user_id:
            raise RelationValidationError(f"{node_type} 节点属于其他用户: {node_id}")

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
