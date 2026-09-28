"""带强制用户作用域的 MemoryRepository。

所有读取接口都必须显式接收 user_id，并在 SQL 查询阶段按 user_id 过滤，
绝不依赖查询后的 Python 过滤。关系写入前必须校验两端节点存在且属于同一用户。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from masm.storage.db import Database
from masm.storage.models import (
    Asset,
    DeletionIntent,
    Memory,
    MemoryConflict,
    MemoryEmbedding,
    MemoryEntity,
    MemoryEvent,
    MemoryRelation,
    ProcessingRun,
    RequestLedger,
    SessionRecord,
    SourceMessage,
    User,
)
from masm.storage.types import (
    AddCommit,
    DeletedRun,
    LedgerState,
    MemoryBundle,
    MemoryCandidate,
    ValidatedActions,
)


def _deleted_rows(session: Session, statement: Any) -> int:
    """执行一条 DELETE 并返回受影响行数。"""
    result = cast(CursorResult[Any], session.execute(statement))
    return int(result.rowcount or 0)


def _image_object_uris(content: Any) -> list[str]:
    """从原始消息内容中提取图片对象地址。"""
    found: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url")
                if isinstance(url, str):
                    found.append(url)
    return found

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
    """幂等账本状态或所有权不符合预期（例如所有权已被接管）。"""


class ActionApplicationError(RuntimeError):
    """治理动作无法安全应用（目标缺失、冲突组不兼容或并发写入冲突）。

    与 :class:`LedgerStateError` 严格区分：这类失败不代表所有权失效，调用方应当用同一个
    owner token 降级提交纯基线记忆束，而不是返回冲突。
    """


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
                    request_id=bundle.request_id,
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
        绝不触碰原始证据、原始消息或既有记忆正文。治理规则不满足时抛
        :class:`ActionApplicationError`，由调用方降级为纯基线提交。
        """
        memory = session.get(Memory, memory_id)
        if memory is None:
            raise ActionApplicationError("待应用动作的记忆不存在")

        target_ids: set[UUID] = {relation.target_id for relation in actions.relations}
        target_ids.update(actions.conflict_targets)
        if actions.supersedes is not None:
            target_ids.add(actions.supersedes)
        if actions.duplicate_of is not None:
            target_ids.add(actions.duplicate_of)
        # 一次性按 memory_id 升序锁定全部治理目标：全局确定顺序，避免死锁。
        locked = self._lock_action_targets(session, user_id, sorted(target_ids, key=str))

        for relation in actions.relations:
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
            memory.supersedes = actions.supersedes
            # 仅修改治理状态：原始证据与正文保持不可变。
            locked[actions.supersedes].status = "superseded"

        if actions.duplicate_of is not None:
            memory.duplicate_of = actions.duplicate_of

        if actions.conflict_group_id is not None:
            self._apply_conflict_group(session, user_id, memory, actions, locked)

    def _lock_action_targets(
        self, session: Session, user_id: str, target_ids: Sequence[UUID]
    ) -> dict[UUID, Memory]:
        """按 memory_id 升序锁定全部治理目标，并在锁后重新读取最新值。"""
        locked: dict[UUID, Memory] = {}
        for target_id in target_ids:
            if target_id in locked:
                continue
            row = session.execute(
                select(Memory)
                .where(Memory.id == target_id, Memory.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None:
                raise ActionApplicationError("治理目标记忆不属于该用户或不存在")
            locked[target_id] = row
        return locked

    def _lock_conflict_group(
        self, session: Session, user_id: str, group_id: UUID
    ) -> list[MemoryConflict]:
        """按 memory_id 升序锁定冲突组既有成员行，串行化成员追加与版本分配。"""
        return list(
            session.execute(
                select(MemoryConflict)
                .where(
                    MemoryConflict.user_id == user_id,
                    MemoryConflict.conflict_group_id == group_id,
                )
                .order_by(MemoryConflict.memory_id)
                .with_for_update()
            ).scalars()
        )

    def _conflict_members(
        self, session: Session, user_id: str, group_id: UUID
    ) -> list[MemoryConflict]:
        """锁后重新读取该组全部成员（新语句 => 新快照，可见并发事务已提交的成员）。"""
        return list(
            session.execute(
                select(MemoryConflict).where(
                    MemoryConflict.user_id == user_id,
                    MemoryConflict.conflict_group_id == group_id,
                )
            ).scalars()
        )

    def _apply_conflict_group(
        self,
        session: Session,
        user_id: str,
        memory: Memory,
        actions: ValidatedActions,
        locked: dict[UUID, Memory],
    ) -> None:
        """在行锁内确定有效冲突组并追加成员。

        目标是复用已确定的组而不是无条件覆盖；版本号必须在取得组锁并用新语句重新
        读取之后才计算，避免并发事务分配出相同版本。
        """
        proposed_group_id = actions.conflict_group_id
        if proposed_group_id is None:
            return

        existing_groups: set[UUID] = set()
        for target_id in actions.conflict_targets:
            target_group_id = locked[target_id].conflict_group_id
            if target_group_id is not None:
                existing_groups.add(target_group_id)
        if len(existing_groups) > 1:
            raise ActionApplicationError("冲突目标分属多个不同的冲突组")
        group_id = next(iter(existing_groups)) if existing_groups else proposed_group_id

        # 串行化点：锁定该组既有成员行，之后才允许分配版本号。
        self._lock_conflict_group(session, user_id, group_id)

        memory.conflict_group_id = group_id
        for target_id in actions.conflict_targets:
            target = locked[target_id]
            if target.conflict_group_id not in (None, group_id):
                raise ActionApplicationError("冲突目标已属于另一个冲突组")
            target.conflict_group_id = group_id
        session.flush()

        members = self._conflict_members(session, user_id, group_id)
        known = {row.memory_id for row in members}
        next_version = max((row.version for row in members), default=0) + 1
        try:
            for member_id in [memory.id, *sorted(set(actions.conflict_targets), key=str)]:
                if member_id in known:
                    continue
                session.add(
                    MemoryConflict(
                        user_id=user_id,
                        conflict_group_id=group_id,
                        memory_id=member_id,
                        version=next_version,
                    )
                )
                next_version += 1
            session.flush()
        except IntegrityError as exc:
            # 唯一约束兜底：并发写入冲突时由调用方降级为基线提交。
            raise ActionApplicationError("冲突组成员并发写入冲突") from exc

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
                duplicate_of=row.Memory.duplicate_of,
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
                    Memory.duplicate_of.label("duplicate_of"),
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
                    scored.c.duplicate_of,
                    best_distance.label("distance"),
                )
                .group_by(
                    scored.c.memory_id,
                    scored.c.user_id,
                    scored.c.summary,
                    scored.c.supersedes,
                    scored.c.status,
                    scored.c.conflict_group_id,
                    scored.c.duplicate_of,
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
                duplicate_of=row.duplicate_of,
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
                duplicate_of=memory.duplicate_of,
            )
            for memory in memories
        ]

    def delete_run(self, user_id: str, request_id: str) -> DeletedRun:
        """删除该用户该运行（request_id）的数据库记录，返回待清理的对象地址。

        所有删除都在 SQL 阶段按 user_id + request_id 过滤，因此不会影响其他用户或
        同一用户的其他运行。对象地址取自该运行的原始消息内容。
        """
        with self._database.session() as session:
            memory_ids = list(
                session.execute(
                    select(Memory.id).where(
                        Memory.user_id == user_id, Memory.request_id == request_id
                    )
                ).scalars()
            )
            messages: list[Any] = list(
                session.execute(
                    select(SourceMessage.content).where(
                        SourceMessage.user_id == user_id,
                        SourceMessage.request_id == request_id,
                    )
                )
                .scalars()
                .all()
            )
            object_uris = sorted(
                {
                    uri
                    for content in messages
                    for uri in _image_object_uris(content)
                }
            )
            # 运行级资产行：只删除属于本次运行的逻辑资产记录。
            assets = _deleted_rows(
                session,
                delete(Asset).where(
                    Asset.user_id == user_id, Asset.request_id == request_id
                ),
            )
            relations = 0
            conflicts = 0
            if memory_ids:
                # 处理指向已删除记忆的悬空治理引用。
                session.execute(
                    update(Memory)
                    .where(Memory.duplicate_of.in_(memory_ids))
                    .values(duplicate_of=None)
                )
                session.execute(
                    update(Memory).where(Memory.supersedes.in_(memory_ids)).values(supersedes=None)
                )
                for model in (MemoryEmbedding, MemoryEntity, MemoryEvent):
                    _deleted_rows(
                        session,
                        delete(model).where(
                            model.user_id == user_id, model.memory_id.in_(memory_ids)
                        ),
                    )
                relations = _deleted_rows(
                    session,
                    delete(MemoryRelation).where(
                        MemoryRelation.user_id == user_id,
                        or_(
                            MemoryRelation.source_id.in_(memory_ids),
                            MemoryRelation.target_id.in_(memory_ids),
                        ),
                    ),
                )
                conflicts = _deleted_rows(
                    session,
                    delete(MemoryConflict).where(
                        MemoryConflict.user_id == user_id,
                        MemoryConflict.memory_id.in_(memory_ids),
                    ),
                )
            memories = _deleted_rows(
                session,
                delete(Memory).where(
                    Memory.user_id == user_id, Memory.request_id == request_id
                ),
            )
            sources = _deleted_rows(
                session,
                delete(SourceMessage).where(
                    SourceMessage.user_id == user_id,
                    SourceMessage.request_id == request_id,
                ),
            )
            ledger = _deleted_rows(
                session,
                delete(RequestLedger).where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                ),
            )
            runs = _deleted_rows(
                session,
                delete(ProcessingRun).where(
                    ProcessingRun.user_id == user_id,
                    ProcessingRun.request_id == request_id,
                ),
            )
            # 会话只在其不再被任何运行引用时才删除。
            for session_id in list(
                session.execute(
                    select(SessionRecord.id).where(SessionRecord.user_id == user_id)
                ).scalars()
            ):
                remaining = int(
                    session.execute(
                        select(func.count()).select_from(Memory).where(
                            Memory.session_id == session_id
                        )
                    ).scalar_one()
                ) + int(
                    session.execute(
                        select(func.count()).select_from(SourceMessage).where(
                            SourceMessage.session_id == session_id
                        )
                    ).scalar_one()
                )
                if remaining == 0:
                    session.execute(delete(SessionRecord).where(SessionRecord.id == session_id))
            session.commit()
            # 只有确认任何用户/运行都不再引用时，物理文件才允许删除。
            still_referenced = (
                set(
                    session.execute(
                        select(Asset.object_uri).where(Asset.object_uri.in_(object_uris))
                    ).scalars()
                )
                if object_uris
                else set()
            )
            del ledger, runs
        return DeletedRun(
            request_id=request_id,
            memories=memories,
            assets=assets,
            sources=sources,
            relations=relations,
            conflicts=conflicts,
            # 仍被任何用户/运行引用的对象不删除，避免影响其他运行。
            object_uris=tuple(uri for uri in object_uris if uri not in still_referenced),
            shared_object_uris=tuple(sorted(still_referenced)),
        )

    def deletable_object_uris(self, user_id: str, request_id: str) -> list[str]:
        """返回该运行独占（无任何其他引用）的对象地址，供删除意图使用。"""
        with self._database.session() as session:
            messages: list[Any] = list(
                session.execute(
                    select(SourceMessage.content).where(
                        SourceMessage.user_id == user_id,
                        SourceMessage.request_id == request_id,
                    )
                ).scalars()
            )
            owned = sorted({uri for content in messages for uri in _image_object_uris(content)})
            if not owned:
                return []
            referenced = set(
                session.execute(
                    select(Asset.object_uri)
                    .where(
                        Asset.object_uri.in_(owned),
                        or_(
                            Asset.user_id != user_id,
                            Asset.request_id.is_(None),
                            Asset.request_id != request_id,
                        ),
                    )
                    .distinct()
                ).scalars()
            )
        return [uri for uri in owned if uri not in referenced]

    def register_deletion_intents(
        self, user_id: str, request_id: str, object_uris: Sequence[str]
    ) -> None:
        """持久化对象删除意图（幂等）：已存在的 URI 保持原状态。"""
        if not object_uris:
            return
        with self._database.session() as session:
            existing = set(
                session.execute(
                    select(DeletionIntent.object_uri).where(
                        DeletionIntent.user_id == user_id,
                        DeletionIntent.request_id == request_id,
                    )
                ).scalars()
            )
            for uri in object_uris:
                if uri in existing:
                    continue
                session.add(
                    DeletionIntent(
                        user_id=user_id,
                        request_id=request_id,
                        object_uri=uri,
                        status="PENDING",
                    )
                )
            session.commit()

    def pending_deletion_uris(self, user_id: str, request_id: str) -> list[str]:
        """返回仍未确认删除的对象地址（跨进程可恢复）。"""
        with self._database.session() as session:
            return list(
                session.execute(
                    select(DeletionIntent.object_uri)
                    .where(
                        DeletionIntent.user_id == user_id,
                        DeletionIntent.request_id == request_id,
                        DeletionIntent.status == "PENDING",
                    )
                    .order_by(DeletionIntent.object_uri)
                ).scalars()
            )

    def mark_deletion_intent(
        self,
        user_id: str,
        request_id: str,
        object_uri: str,
        *,
        done: bool,
        error: str | None = None,
    ) -> None:
        """把删除意图标记为完成，或累加一次失败尝试。"""
        with self._database.session() as session:
            intent = session.execute(
                select(DeletionIntent).where(
                    DeletionIntent.user_id == user_id,
                    DeletionIntent.request_id == request_id,
                    DeletionIntent.object_uri == object_uri,
                )
            ).scalar_one_or_none()
            if intent is None:
                return
            if done:
                intent.status = "DONE"
                intent.last_error = None
            else:
                intent.attempts = intent.attempts + 1
                intent.last_error = (error or "unknown")[:255]
            session.commit()

    def conflict_peers(
        self, user_id: str, memory_ids: Sequence[UUID], limit: int
    ) -> list[MemoryCandidate]:
        """冲突组补全：返回同组其它成员，SQL 查询阶段即按 user_id 过滤。"""
        ids = list(memory_ids)
        if not ids:
            return []
        with self._database.session() as session:
            groups = list(
                session.execute(
                    select(Memory.conflict_group_id).where(
                        Memory.user_id == user_id,
                        Memory.id.in_(ids),
                        Memory.conflict_group_id.is_not(None),
                    )
                ).scalars()
            )
            if not groups:
                return []
            rows = (
                session.execute(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        Memory.conflict_group_id.in_(list(set(groups))),
                        Memory.id.not_in(ids),
                    )
                    .order_by(Memory.observed_at, Memory.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return [
            MemoryCandidate(
                memory_id=row.id,
                user_id=row.user_id,
                content=row.summary,
                score=0.0,
                supersedes=row.supersedes,
                status=row.status,
                conflict_group_id=row.conflict_group_id,
                duplicate_of=row.duplicate_of,
            )
            for row in rows
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
                duplicate_of=row.duplicate_of,
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
