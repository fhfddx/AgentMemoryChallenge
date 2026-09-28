"""带强制用户作用域的 MemoryRepository。

所有读取接口都必须显式接收 user_id，并在 SQL 查询阶段按 user_id 过滤，
绝不依赖查询后的 Python 过滤。关系写入前必须校验两端节点存在且属于同一用户。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from masm.storage.db import Database
from masm.storage.models import (
    Asset,
    Memory,
    MemoryConflict,
    MemoryEmbedding,
    MemoryEntity,
    MemoryEvent,
    MemoryRelation,
    RequestLedger,
    SessionRecord,
    SourceMessage,
    User,
)
from masm.storage.types import (
    AddCommit,
    LedgerState,
    MemoryBundle,
    MemoryCandidate,
    ValidatedActions,
)

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
        self, user_id: str, request_id: str, bundle: MemoryBundle, *, owner_token: datetime
    ) -> AddCommit:
        """在单个数据库事务中完成最终提交。

        事务边界：锁定 RequestLedger -> 原子校验 owner_token 且状态为 PROCESSING ->
        写入 Session/Asset/SourceMessage/Memory -> 账本置 COMMITTED（保留 owner_token，
        供提交后崩溃重试找到并发布正确所有者的暂存对象）-> 一次性 commit。
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
            if ledger.updated_at != owner_token:
                raise LedgerStateError("所有者标识已失效，拒绝提交")
            memory_ids = self._write_bundle(session, user_id, bundle)
            # 显式写入 updated_at，既保留 fencing token，也避免 onupdate 覆盖它。
            session.execute(
                update(RequestLedger)
                .where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
                .values(status="COMMITTED", updated_at=owner_token)
            )
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
        session_model_id = self._ensure_session_id(session, user_id, bundle.session_id)
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
                    session_id=session_model_id,
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
                session_id=session_model_id,
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
            for draft_embedding in (draft.embedding, *draft.image_embeddings):
                if draft_embedding is None:
                    continue
                session.add(
                    MemoryEmbedding(
                        user_id=user_id,
                        memory_id=memory.id,
                        modality=draft_embedding.modality,
                        model_name=draft_embedding.model_name,
                        model_version=draft_embedding.model_version,
                        dimensions=draft_embedding.dimensions,
                        vector=list(draft_embedding.vector),
                    )
                )
        if bundle.actions is not None and memory_ids:
            self._apply_actions(session, user_id, memory_ids[0], bundle.actions)
        return memory_ids

    def _apply_actions(
        self,
        session: Session,
        user_id: str,
        memory_id: UUID,
        actions: ValidatedActions,
    ) -> None:
        """按 ValidatedActions 写入治理字段、关系边与冲突组。

        只允许修改治理状态（duplicate_of / supersedes / conflict_group_id / status）；
        绝不触碰原始证据、原始消息或既有记忆正文。
        """
        memory = session.get(Memory, memory_id)
        if memory is None:
            raise LedgerStateError("待应用动作的记忆不存在")

        memory.duplicate_of = actions.duplicate_of
        memory.supersedes = actions.supersedes
        if actions.conflict_group_id is not None:
            memory.conflict_group_id = actions.conflict_group_id

        for relation in actions.relations:
            target = session.get(Memory, relation.target_id)
            if target is None or target.user_id != user_id:
                raise LedgerStateError("关系目标不属于该用户")
            session.add(
                MemoryRelation(
                    user_id=user_id,
                    source_type="memory",
                    source_id=memory_id,
                    target_type="memory",
                    target_id=relation.target_id,
                    relation_type=relation.relation_type,
                    confidence=relation.confidence,
                    evidence_ref=(
                        dict(relation.evidence_ref) if relation.evidence_ref is not None else None
                    ),
                )
            )

        if actions.supersedes is not None:
            superseded = session.get(Memory, actions.supersedes)
            if superseded is None or superseded.user_id != user_id:
                raise LedgerStateError("替代目标不属于该用户")
            # 仅修改治理状态：原始证据与正文保持不可变。
            superseded.status = "superseded"

        if actions.conflict_group_id is not None:
            members = [memory_id]
            for target_id in actions.conflict_targets:
                target = session.get(Memory, target_id)
                if target is None or target.user_id != user_id:
                    raise LedgerStateError("冲突目标不属于该用户")
                # 延续已有冲突组：目标原属的组必须与本次组一致。
                if target.conflict_group_id not in (None, actions.conflict_group_id):
                    raise LedgerStateError("冲突目标已属于另一个冲突组")
                target.conflict_group_id = actions.conflict_group_id
                members.append(target_id)
            session.flush()
            # 已有成员不得重复插入；新成员版本号在该组现有最大版本之后追加。
            registered = session.execute(
                select(MemoryConflict).where(
                    MemoryConflict.user_id == user_id,
                    MemoryConflict.conflict_group_id == actions.conflict_group_id,
                )
            ).scalars().all()
            known = {row.memory_id for row in registered}
            next_version = max((row.version for row in registered), default=0) + 1
            for member_id in dict.fromkeys(members):
                if member_id in known:
                    continue
                session.add(
                    MemoryConflict(
                        user_id=user_id,
                        conflict_group_id=actions.conflict_group_id,
                        memory_id=member_id,
                        version=next_version,
                    )
                )
                next_version += 1

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
        """返回幂等账本状态与所有者标识（fencing token）。"""
        with self._database.session() as session:
            ledger = session.execute(
                select(RequestLedger).where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
            ).scalar_one_or_none()
        if ledger is None:
            return None
        return LedgerState(status=ledger.status, owner_token=ledger.updated_at)

    def claim_request(
        self, user_id: str, request_id: str, session_id: str, *, now: datetime
    ) -> datetime | None:
        """原子地写入 PROCESSING 账本；返回本次所有者标识，已被占用时返回 None。"""
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
                return None
        return now

    def takeover_request(
        self,
        user_id: str,
        request_id: str,
        *,
        now: datetime,
        stale_before: datetime,
    ) -> datetime | None:
        """原子接管已过期的 PROCESSING 租约；返回新所有者标识，失败返回 None。"""
        with self._database.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(RequestLedger)
                    .where(
                        RequestLedger.user_id == user_id,
                        RequestLedger.request_id == request_id,
                        RequestLedger.status == "PROCESSING",
                        RequestLedger.updated_at < stale_before,
                    )
                    .values(updated_at=now)
                ),
            )
            session.commit()
            claimed = result.rowcount == 1
        return now if claimed else None

    def mark_request(
        self, user_id: str, request_id: str, status: str, *, owner_token: datetime
    ) -> bool:
        """按所有者标识原子更新账本状态；token 不匹配时不产生任何影响。"""
        with self._database.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(RequestLedger)
                    .where(
                        RequestLedger.user_id == user_id,
                        RequestLedger.request_id == request_id,
                        RequestLedger.status == "PROCESSING",
                        RequestLedger.updated_at == owner_token,
                    )
                    .values(status=status, updated_at=owner_token)
                ),
            )
            session.commit()
            updated = result.rowcount == 1
        return updated

    def has_committed_data(self, user_id: str, request_id: str) -> bool:
        """该 request_id 是否已经有已提交的记忆数据。"""
        with self._database.session() as session:
            count = session.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.user_id == user_id, Memory.request_id == request_id)
            ).scalar_one()
        return bool(count)

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
                supersedes=row.Memory.supersedes,
                status=row.Memory.status,
                conflict_group_id=row.Memory.conflict_group_id,
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
        """向量检索：调用方必须明确目标向量空间（模态 + 模型 + 维度）。

        每个 memory_id 最多返回一次：先按 memory_id 聚合出该记忆的最佳（最小）余弦距离，
        再按距离排序并应用 limit，避免同一记忆的多条图片向量重复占用召回名额。
        """
        query_vector = list(vector)
        dimensions = len(query_vector)
        with self._database.session() as session:
            distance = MemoryEmbedding.vector.cosine_distance(query_vector).label("distance")
            scored = (
                select(
                    Memory.id.label("memory_id"),
                    Memory.user_id.label("user_id"),
                    Memory.summary.label("summary"),
                    Memory.supersedes.label("supersedes"),
                    Memory.status.label("status"),
                    Memory.conflict_group_id.label("conflict_group_id"),
                    distance,
                )
                .join(MemoryEmbedding, MemoryEmbedding.memory_id == Memory.id)
                .where(
                    Memory.user_id == user_id,
                    MemoryEmbedding.user_id == user_id,
                    MemoryEmbedding.modality == modality,
                    MemoryEmbedding.model_name == model_name,
                    MemoryEmbedding.model_version == model_version,
                    MemoryEmbedding.dimensions == dimensions,
                )
                .subquery()
            )
            best_distance = func.min(scored.c.distance)
            stmt = (
                select(
                    scored.c.memory_id,
                    scored.c.user_id,
                    scored.c.summary,
                    scored.c.supersedes,
                    scored.c.status,
                    scored.c.conflict_group_id,
                    best_distance.label("distance"),
                )
                .group_by(
                    scored.c.memory_id,
                    scored.c.user_id,
                    scored.c.summary,
                    scored.c.supersedes,
                    scored.c.status,
                    scored.c.conflict_group_id,
                )
                .order_by(best_distance, scored.c.memory_id)
                .limit(limit)
            )
            rows = session.execute(stmt).all()
        return [
            MemoryCandidate(
                memory_id=row.memory_id,
                user_id=row.user_id,
                content=row.summary,
                score=1.0 - float(row.distance),
                supersedes=row.supersedes,
                status=row.status,
                conflict_group_id=row.conflict_group_id,
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
                supersedes=memory.supersedes,
                status=memory.status,
                conflict_group_id=memory.conflict_group_id,
            )
            for memory in memories
        ]

    def metadata_candidates(
        self,
        user_id: str,
        *,
        modality: str | None = None,
        keywords: Sequence[str] = (),
        limit: int,
    ) -> list[MemoryCandidate]:
        """元数据召回：按模态与关键词过滤，SQL 查询阶段即按 user_id 过滤。"""
        with self._database.session() as session:
            statement = select(Memory).where(Memory.user_id == user_id)
            if modality is not None:
                statement = statement.where(Memory.modality == modality)
            if keywords:
                statement = statement.where(
                    Memory.keywords.op("?|")(pg_array(list(keywords)))
                )
            statement = statement.order_by(Memory.observed_at.desc(), Memory.id).limit(limit)
            rows = session.execute(statement).scalars().all()
        return [
            MemoryCandidate(
                memory_id=row.id,
                user_id=row.user_id,
                content=row.summary,
                score=0.0,
                supersedes=row.supersedes,
                status=row.status,
                conflict_group_id=row.conflict_group_id,
            )
            for row in rows
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
        """按需创建用户（幂等且并发安全）。"""
        session.execute(
            pg_insert(User).values(user_id=user_id).on_conflict_do_nothing(
                index_elements=["user_id"]
            )
        )

    def _ensure_session_id(self, session: Session, user_id: str, session_id: str) -> UUID:
        """按需创建会话并返回其主键（幂等且并发安全）。"""
        statement = (
            pg_insert(SessionRecord)
            .values(user_id=user_id, session_id=session_id)
            .on_conflict_do_update(
                index_elements=["user_id", "session_id"],
                set_={"session_id": session_id},
            )
            .returning(SessionRecord.id)
        )
        return session.execute(statement).scalar_one()
