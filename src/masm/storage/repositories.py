"""带强制用户作用域的 MemoryRepository。

所有读取接口都必须显式接收 user_id，并在 SQL 查询阶段按 user_id 过滤，
绝不依赖查询后的 Python 过滤。关系写入前必须校验两端节点存在且属于同一用户。
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import Connection, CursorResult, Engine, delete, func, or_, select, text, update
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
    ObjectDeletionOutcome,
    ValidatedActions,
)


def _deleted_rows(session: Session, statement: Any) -> int:
    """执行一条 DELETE 并返回受影响行数。"""
    result = cast(CursorResult[Any], session.execute(statement))
    return int(result.rowcount or 0)


def _memory_granularity(value: str) -> Literal["context", "message"]:
    """把数据库约束所允许的粒度收窄成领域类型。"""
    if value not in ("context", "message"):
        raise ValueError("数据库记忆粒度无效")
    return cast(Literal["context", "message"], value)


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


# 对象级 advisory 锁：写入方（Add）与删除方共用同一把锁，串行化「引用检查 -> 物理删除」。
# 写入方用事务级锁（随写入事务释放）；删除方需要跨多个事务（提交行删除 -> 物理删除 ->
# 标记 DONE），因此用会话级锁 + 独占连接。
_OBJECT_XACT_LOCK_STATEMENT = "SELECT pg_advisory_xact_lock(hashtext(:object_uri)::bigint)"
_OBJECT_LOCK_STATEMENT = "SELECT pg_advisory_lock(hashtext(:object_uri)::bigint)"
_OBJECT_UNLOCK_STATEMENT = "SELECT pg_advisory_unlock(hashtext(:object_uri)::bigint)"

# 尝试获取对象锁前必须先设置 lock_timeout，否则可能无限等待持锁的并发写入方。
_LOCK_TIMEOUT_SECONDS = 30

# 运行级生命周期锁：Add 的「finalize -> publish」与 Delete 的「删行 -> 删对象 -> 清暂存」
# 必须互斥，否则删除完成后旧 Add 仍能把暂存文件重新发布成孤儿对象。
# 与对象锁使用不同的键空间（加前缀）；分隔符必须避开 NUL（PostgreSQL text 不接受 0x00）。
_LIFECYCLE_LOCK_PREFIX = "lifecycle\x1f"
_LIFECYCLE_LOCK_SEPARATOR = "\x1f"

# 内部错误码：删除意图登记为 PENDING、等待物理删除（绝不写外部/自由文本）。
INTERNAL_ERROR_PENDING_PHYSICAL_DELETE = "pending_physical_delete"
# 内部错误码：暂存目录清理失败，需要重试（合规删除不得静默吞错）。
INTERNAL_ERROR_PENDING_STAGING_CLEANUP = "pending_staging_cleanup"


def lifecycle_lock_key(user_id: str, request_id: str) -> str:
    """运行级生命周期锁的键（同一 user_id + request_id 的 Add 与 Delete 共用）。"""
    return f"{_LIFECYCLE_LOCK_PREFIX}{user_id}{_LIFECYCLE_LOCK_SEPARATOR}{request_id}"


# 暂存清理意图的地址前缀：它不是对象地址，而是「该运行该所有者的暂存目录」。
_STAGING_URI_PREFIX = "staging://"


def staging_uri_for(owner_token: datetime) -> str:
    """把所有者标识编码成暂存清理意图地址（沿用 owner_token 隔离语义）。"""
    return f"{_STAGING_URI_PREFIX}{_owner_token_text(owner_token)}"


def is_staging_uri(uri: str) -> bool:
    """该删除意图地址是否代表暂存目录清理。"""
    return uri.startswith(_STAGING_URI_PREFIX)


def parse_staging_uri(uri: str) -> datetime:
    """从暂存清理意图地址还原所有者标识（时间统一按 UTC 解析）。"""
    if not is_staging_uri(uri):
        raise ValueError("不是暂存清理意图地址")
    return datetime.strptime(
        uri[len(_STAGING_URI_PREFIX) :], "%Y%m%dT%H%M%S.%f"
    ).replace(tzinfo=UTC)


def _owner_token_text(owner_token: datetime) -> str:
    """所有者标识的稳定文本形式（与 AssetStore 的暂存命名保持一致，统一到 UTC）。"""
    if owner_token.tzinfo is None:
        owner_token = owner_token.replace(tzinfo=UTC)
    return owner_token.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")


def _referencing_object_uris(
    session: Session, user_id: str, request_id: str, object_uris: Sequence[str]
) -> set[str]:
    """返回仍被其他用户或其他运行引用的对象地址（权威存活性检查）。

    两个来源都要看，缺一不可：
    - ``assets``：运行级资产行（``request_id`` 为 NULL 是迁移未回填的遗留行，必须算作
      仍被引用：``Asset.request_id != request_id`` 对 NULL 求值为 NULL，并不为真）；
    - ``source_messages``：原始消息里出现的对象地址。旧库中同一对象可能被多次运行引用
      而只有一行资产记录，因此只信资产行会把「仍被其他运行引用」的对象误判为独占。
    """
    if not object_uris:
        return set()
    owned = list(object_uris)
    asset_refs = set(
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
    message_refs: set[str] = set()
    other_run_messages: list[Any] = list(
        session.execute(
            select(SourceMessage.content).where(
                SourceMessage.user_id == user_id,
                SourceMessage.request_id != request_id,
            )
        ).scalars()
    )
    for raw_content in other_run_messages:
        message_refs.update(_image_object_uris(raw_content))
    return asset_refs | (message_refs & set(owned))


def lock_object_uris_in_transaction(session: Session, object_uris: Sequence[str]) -> None:
    """在**当前事务内**按字典序锁定对象地址（写入方使用，提交/回滚时自动释放）。

    ``pg_advisory_xact_lock`` 与删除方使用的会话级锁是同一把锁：写入方提交之前，删除方
    无法取得锁，因此不可能把刚写入的对象当成「无人引用」删掉。
    """
    for object_uri in sorted(set(object_uris)):
        session.execute(text(_OBJECT_XACT_LOCK_STATEMENT), {"object_uri": object_uri})


def _advisory_lock_keys(keys: Sequence[str]) -> list[str]:
    """把任意键转换为 advisory 锁键，保持稳定顺序。"""
    return sorted(set(keys))


@contextmanager
def _session_locks(
    connection: Connection,
    keys: Sequence[str],
    *,
    lock_timeout_seconds: int | None = None,
) -> Iterator[None]:
    """在给定连接上取得并最终释放这些会话级 advisory 锁。

    会话级锁必须跨事务：``delete_run`` 的行删除、物理删除与状态标记是多个独立事务，
    事务级锁会在第一个 commit 处失效。``lock_timeout`` 在**尝试加锁之前**设置，连接归还
    连接池前复位，因此持锁的并发写入方不会让删除方无限等待，也不会污染后续使用者。
    """
    timeout = _LOCK_TIMEOUT_SECONDS if lock_timeout_seconds is None else int(lock_timeout_seconds)
    connection.execute(text(f"SET lock_timeout = '{timeout}s'"))
    connection.commit()
    acquired: list[str] = []
    try:
        for key in _advisory_lock_keys(keys):
            connection.execute(text(_OBJECT_LOCK_STATEMENT), {"object_uri": key})
            connection.commit()
            acquired.append(key)
        yield
    finally:
        for key in reversed(acquired):
            with suppress(Exception):
                connection.rollback()
                connection.execute(text(_OBJECT_UNLOCK_STATEMENT), {"object_uri": key})
                connection.commit()
        with suppress(Exception):
            connection.rollback()
            connection.execute(text("RESET lock_timeout"))
            connection.commit()


def lock_object_uris(
    connection: Connection,
    object_uris: Sequence[str],
    *,
    lock_timeout_seconds: int | None = None,
) -> AbstractContextManager[None]:
    """锁住这批对象地址（删除方使用；调用方需提供独占连接）。"""
    return _session_locks(
        connection, list(object_uris), lock_timeout_seconds=lock_timeout_seconds
    )


@contextmanager
def lock_run_lifecycle(
    connection: Connection,
    user_id: str,
    request_id: str,
    *,
    lock_timeout_seconds: int | None = None,
) -> Iterator[None]:
    """运行级生命周期锁：覆盖 Add 的「finalize -> publish」与 Delete 的整段删除。

    锁顺序约定为「先生命周期锁，后对象锁」，两侧一致，因此不会死锁。
    """
    with _session_locks(
        connection,
        [lifecycle_lock_key(user_id, request_id)],
        lock_timeout_seconds=lock_timeout_seconds,
    ):
        yield


def try_lock_run_lifecycle(
    engine: Engine,
    user_id: str,
    request_id: str,
    *,
    lock_timeout_seconds: int = 2,
) -> bool:
    """用独立连接尝试取得运行级生命周期锁（供测试与诊断使用）。"""
    with engine.connect() as connection:
        try:
            with lock_run_lifecycle(
                connection,
                user_id,
                request_id,
                lock_timeout_seconds=lock_timeout_seconds,
            ):
                pass
        except Exception:
            connection.rollback()
            return False
    return True


def try_lock_object_uris(
    engine: Engine, object_uris: Sequence[str], *, lock_timeout_seconds: int = 2
) -> bool:
    """用独立连接尝试锁定对象地址；被其他连接持有时返回 False（供测试与诊断使用）。"""
    with engine.connect() as connection:
        try:
            with lock_object_uris(
                connection, object_uris, lock_timeout_seconds=lock_timeout_seconds
            ):
                pass
        except Exception:
            connection.rollback()
            return False
    return True


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


@dataclass(frozen=True)
class _IntentTarget:
    """一条待物理删除的意图目标（从持久化删除意图读取，不依赖原始消息）。"""

    user_id: str
    request_id: str
    object_uri: str


class MemoryRepository:
    """记忆存储仓库。"""

    def __init__(
        self,
        database: Database,
        *,
        commit: Callable[[Session], None] | None = None,
    ) -> None:
        """``commit`` 是提交点注入（测试用于模拟提交失败，默认调用 ``Session.commit``）。"""
        self._database = database
        self._commit = commit or (lambda session: session.commit())

    def lifecycle_connection(self) -> Connection:
        """用于持有所在运行生命周期锁的独占连接（调用方负责关闭）。"""
        return self._database.engine.connect()

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
        for index, draft in enumerate(bundle.memories):
            if draft.granularity == "context":
                if index != 0 or draft.source_position is not None:
                    raise ValueError("context 记忆必须位于首项且无来源位置")
            elif draft.granularity == "message":
                if index == 0 or draft.source_position is None or draft.source_position < 0:
                    raise ValueError("message 记忆必须跟在 context 后且来源位置非负")
            else:
                raise ValueError("未知记忆粒度")
        self._ensure_user(session, user_id)
        session_model_id = self._ensure_session_id(session, user_id, bundle.session_id)
        if bundle.assets:
            # 与运行级删除共用同一把对象锁：本事务提交前，删除方无法确认「无人引用」
            # 并物理删除这些对象，因此不会出现「刚写入的对象被并发删除」。
            lock_object_uris_in_transaction(session, [asset.object_uri for asset in bundle.assets])
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
                granularity=draft.granularity,
                source_position=draft.source_position,
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
                granularity=_memory_granularity(row.Memory.granularity),
                request_id=row.Memory.request_id,
                source_position=row.Memory.source_position,
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
                    Memory.granularity.label("granularity"),
                    Memory.request_id.label("request_id"),
                    Memory.source_position.label("source_position"),
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
                    scored.c.granularity,
                    scored.c.request_id,
                    scored.c.source_position,
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
                    scored.c.granularity,
                    scored.c.request_id,
                    scored.c.source_position,
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
                granularity=_memory_granularity(row.granularity),
                request_id=row.request_id,
                source_position=row.source_position,
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
                granularity=_memory_granularity(memory.granularity),
                request_id=memory.request_id,
                source_position=memory.source_position,
            )
            for memory in memories
        ]

    def delete_run(self, user_id: str, request_id: str) -> DeletedRun:
        """删除该用户该运行（request_id）的数据库记录，并把待删对象登记为 PENDING 意图。

        本方法**只做可回滚的数据库工作**，绝不触碰文件系统：删行与删除意图在同一个事务中
        提交，因此事务回滚时既不会丢数据，也不会提前删掉物理对象。

        对象地址来自该运行的原始消息；行删除完成后在同一事务内用权威存活性检查
        （资产行 + 原始消息）确认哪些对象仍被其他用户/运行引用，未登记的共享对象绝不会
        进入 PENDING。物理删除由 :meth:`retry_pending_object_deletions` 完成。
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
                {uri for content in messages for uri in _image_object_uris(content)}
            )
            # 删除账本前先取出所有者标识：暂存目录按 owner_token 隔离，删掉账本就再也
            # 无法定位该运行的暂存私有图片（合规删除必须能清理它们）。
            owner_tokens = self._ledger_owner_tokens(session, user_id, request_id)
            # 这里不加对象锁：本阶段只做可回滚的数据库工作，锁竞争不应该让删行失败。
            # 对象锁只保护「重检引用 -> 物理删除」这一段（见 retry_pending_object_deletions），
            # 因此并发写入要么在重检时被看到（对象保留），要么因持锁而排在物理删除之后。
            return self._delete_run_rows(
                session, user_id, request_id, memory_ids, object_uris, owner_tokens
            )

    def _ledger_owner_tokens(
        self, session: Session, user_id: str, request_id: str
    ) -> list[datetime]:
        """读取该运行账本记录的所有者标识（暂存目录命名依据），删除前调用。"""
        return list(
            session.execute(
                select(RequestLedger.updated_at).where(
                    RequestLedger.user_id == user_id,
                    RequestLedger.request_id == request_id,
                )
            ).scalars()
        )

    def _delete_run_rows(
        self,
        session: Session,
        user_id: str,
        request_id: str,
        memory_ids: list[UUID],
        object_uris: Sequence[str],
        owner_tokens: Sequence[datetime] = (),
    ) -> DeletedRun:
        """执行可回滚的行删除与 PENDING 意图登记（提交由本方法完成）。"""
        # 运行级资产行：只删除属于本次运行的逻辑资产记录。
        assets = _deleted_rows(
            session,
            delete(Asset).where(Asset.user_id == user_id, Asset.request_id == request_id),
        )
        relations = 0
        conflicts = 0
        if memory_ids:
            # 处理指向已删除记忆的悬空治理引用；必须按 user_id 限定，
            # 否则会清空其他用户记忆上的 duplicate_of / supersedes。
            session.execute(
                update(Memory)
                .where(
                    Memory.user_id == user_id,
                    Memory.duplicate_of.in_(memory_ids),
                )
                .values(duplicate_of=None)
            )
            session.execute(
                update(Memory)
                .where(
                    Memory.user_id == user_id,
                    Memory.supersedes.in_(memory_ids),
                )
                .values(supersedes=None)
            )
            for model in (MemoryEmbedding, MemoryEntity, MemoryEvent):
                _deleted_rows(
                    session,
                    delete(model).where(model.user_id == user_id, model.memory_id.in_(memory_ids)),
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
            delete(Memory).where(Memory.user_id == user_id, Memory.request_id == request_id),
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
                    select(func.count()).select_from(Memory).where(Memory.session_id == session_id)
                ).scalar_one()
            ) + int(
                session.execute(
                    select(func.count())
                    .select_from(SourceMessage)
                    .where(SourceMessage.session_id == session_id)
                ).scalar_one()
            )
            if remaining == 0:
                session.execute(delete(SessionRecord).where(SessionRecord.id == session_id))

        # 行删除之后、同一事务内确认：只有任何用户/运行都不再引用的对象才登记。
        shared = _referencing_object_uris(session, user_id, request_id, object_uris)
        if shared:
            self._ensure_user(session, user_id)
        for object_uri in object_uris:
            if object_uri in shared:
                continue
            self._upsert_deletion_intent(
                session,
                user_id,
                request_id,
                object_uri,
                status="PENDING",
                bump_attempts=False,
                error=INTERNAL_ERROR_PENDING_PHYSICAL_DELETE,
            )
        # 暂存目录同样登记为待清理意图：publish 失败时暂存里可能残留私有图片，
        # 合规删除必须清理它们，且清理失败要能被重试而不是静默吞掉。
        for owner_token in owner_tokens:
            self._upsert_deletion_intent(
                session,
                user_id,
                request_id,
                staging_uri_for(owner_token),
                status="PENDING",
                bump_attempts=False,
                error=INTERNAL_ERROR_PENDING_STAGING_CLEANUP,
            )
        # 提交前不得有任何物理删除：提交失败即整体回滚（含意图）。
        self._commit(session)
        del ledger, runs
        return DeletedRun(
            request_id=request_id,
            memories=memories,
            assets=assets,
            sources=sources,
            relations=relations,
            conflicts=conflicts,
            object_uris=tuple(uri for uri in object_uris if uri not in shared),
            shared_object_uris=tuple(sorted(shared)),
            staging_uris=tuple(
                staging_uri_for(owner_token) for owner_token in owner_tokens
            ),
        )

    def retry_pending_object_deletions(
        self,
        user_id: str,
        request_id: str,
        delete_object: Callable[[str], bool],
        cleanup_staging: Callable[[str], bool],
    ) -> ObjectDeletionOutcome:
        """对已持久化的 PENDING 删除意图重试物理删除。

        **不依赖 SourceMessage 推导 URI**：第一次删除失败后原始消息已随运行一起删除，
        因此这里唯一可信的来源是 `deletion_intents` 中的 PENDING 行。

        顺序保证：
        1. 暂存清理只在**该运行的全部对象意图都已终结**后才执行：对象删除失败时暂存里的
           原始图片仍是重放/重试所需的数据，不能先删；
        2. 对象在会话级对象锁下重新做权威存活性检查（资产行 + 原始消息），仍被引用则保留
           PENDING 并跳过，绝不删除共享对象；
        3. 取得并重新校验通过后才执行不可回滚的物理删除；成功后用同一持锁连接把意图标记为
           DONE（失败则累加 attempts 保持 PENDING）。
        """
        pending = self.pending_deletion_intents(user_id, request_id)
        if not pending:
            return ObjectDeletionOutcome()
        object_targets = [row for row in pending if not is_staging_uri(row.object_uri)]
        staging_targets = [row for row in pending if is_staging_uri(row.object_uri)]
        outcomes = {
            row.object_uri: _IntentTarget(
                user_id=row.user_id, request_id=row.request_id, object_uri=row.object_uri
            )
            for row in object_targets
        }

        with self._database.engine.connect() as connection:
            deleted = 0
            missing = 0
            failed: list[tuple[str, str]] = []
            live: set[str] = set()
            with lock_object_uris(connection, sorted(outcomes)):
                live = self._shared_object_uris_for_intents(connection, user_id, outcomes)
                # 仍被任何用户/运行引用的对象：标记 DONE（无需再删），绝不删除物理对象。
                for object_uri in sorted(live):
                    self._settle_intent_on_connection(
                        connection, user_id, request_id, object_uri, done=True, error=""
                    )
                deletable = {uri: t for uri, t in outcomes.items() if uri not in live}
                for object_uri in sorted(deletable):
                    target = deletable[object_uri]
                    try:
                        removed = delete_object(object_uri)
                    except Exception as exc:
                        failed.append((object_uri, type(exc).__name__))
                        continue
                    if removed:
                        deleted += 1
                    else:
                        missing += 1
                    # 状态与物理删除在同一持锁连接上提交。
                    self._settle_intent_on_connection(
                        connection,
                        target.user_id,
                        target.request_id,
                        target.object_uri,
                        done=True,
                        error="",
                    )
                for object_uri, reason in failed:
                    target = deletable[object_uri]
                    self._settle_intent_on_connection(
                        connection,
                        target.user_id,
                        target.request_id,
                        target.object_uri,
                        done=False,
                        error=reason,
                    )
                # 物理删除与意图状态必须一起对临界区可见：在释放对象锁之前提交。
                connection.commit()

            staging_failed: list[tuple[str, str]] = []
            staging_cleaned = 0
            # 对象未删完时不清理暂存：暂存里的原始图片仍是重试/重放所必需的数据。
            if not failed:
                for row in staging_targets:
                    object_uri = row.object_uri
                    try:
                        removed = cleanup_staging(object_uri)
                    except Exception as exc:
                        staging_failed.append((object_uri, type(exc).__name__))
                        continue
                    if removed:
                        staging_cleaned += 1
                    self._settle_intent_on_connection(
                        connection,
                        row.user_id,
                        row.request_id,
                        object_uri,
                        done=True,
                        error="",
                    )
                for object_uri, reason in staging_failed:
                    self._settle_intent_on_connection(
                        connection,
                        user_id,
                        request_id,
                        object_uri,
                        done=False,
                        error=reason,
                    )
        return ObjectDeletionOutcome(
            deleted=deleted,
            missing=missing,
            failed=tuple([*failed, *staging_failed]),
            still_referenced=tuple(sorted(live)),
            staging_cleaned=staging_cleaned,
            staging_failed=tuple(staging_failed),
        )

    def _shared_object_uris_for_intents(
        self,
        connection: Connection,
        user_id: str,
        targets: dict[str, "_IntentTarget"],
    ) -> set[str]:
        """在对象锁内重新检查这些对象是否仍被任何用户/运行引用。

        查询复用持锁连接（不新开连接，避免「持锁等自己」）。运行本身的行已经删除，因此
        按「用户 + 运行」过滤已无意义：这里检查除本运行意图之外的一切引用——其他资产行
        （含 request_id 为 NULL 的遗留行）与其他运行原始消息中出现的对象地址。
        """
        uris = sorted(targets)
        if not uris:
            return set()
        request_ids = sorted({t.request_id for t in targets.values()})
        asset_refs = set(
            connection.execute(
                select(Asset.object_uri)
                .where(
                    Asset.object_uri.in_(uris),
                    or_(
                        Asset.user_id != user_id,
                        Asset.request_id.is_(None),
                        Asset.request_id.not_in(request_ids),
                    ),
                )
                .distinct()
            ).scalars()
        )
        message_rows: list[Any] = list(
            connection.execute(
                select(SourceMessage.content).where(
                    SourceMessage.user_id == user_id,
                    SourceMessage.request_id.not_in(request_ids),
                )
            ).scalars()
        )
        message_refs: set[str] = set()
        for raw_content in message_rows:
            message_refs.update(_image_object_uris(raw_content))
        return asset_refs | (message_refs & set(uris))

    def _settle_intent_on_connection(
        self,
        connection: Connection,
        user_id: str,
        request_id: str,
        object_uri: str,
        *,
        done: bool,
        error: str,
    ) -> None:
        """在持锁连接上推进删除意图状态，并立即提交（物理删除已经发生）。"""
        statement = (
            pg_insert(DeletionIntent)
            .values(
                id=uuid4(),
                user_id=user_id,
                request_id=request_id,
                object_uri=object_uri,
                status="DONE" if done else "PENDING",
                attempts=0 if done else 1,
                last_error=None if done else (error or "unknown")[:255],
            )
            .on_conflict_do_update(
                index_elements=[
                    DeletionIntent.user_id,
                    DeletionIntent.request_id,
                    DeletionIntent.object_uri,
                ],
                set_={
                    "status": "DONE" if done else "PENDING",
                    "attempts": DeletionIntent.attempts + (0 if done else 1),
                    "last_error": None if done else (error or "unknown")[:255],
                    "updated_at": func.now(),
                },
            )
        )
        connection.execute(statement)
        connection.commit()

    def _upsert_deletion_intent(
        self,
        session: Session,
        user_id: str,
        request_id: str,
        object_uri: str,
        *,
        status: str,
        bump_attempts: bool,
        error: str,
    ) -> None:
        """在给定事务内插入或更新一条删除意图（不提交）。"""
        intent = session.execute(
            select(DeletionIntent).where(
                DeletionIntent.user_id == user_id,
                DeletionIntent.request_id == request_id,
                DeletionIntent.object_uri == object_uri,
            )
        ).scalar_one_or_none()
        if intent is None:
            session.add(
                DeletionIntent(
                    user_id=user_id,
                    request_id=request_id,
                    object_uri=object_uri,
                    status=status,
                    attempts=1 if bump_attempts else 0,
                    last_error=(error or None),
                )
            )
            session.flush()
            return
        intent.status = status
        if bump_attempts:
            intent.attempts = intent.attempts + 1
        intent.last_error = (error or None) if error else None
        session.flush()

    def _settle_deletion_intent(
        self,
        user_id: str,
        request_id: str,
        object_uri: str,
        *,
        done: bool,
        error: str,
        session: Session | None = None,
    ) -> None:
        """推进删除意图状态（物理删除已经发生，因此状态必须落库）。

        传入 ``session`` 时复用调用方的持锁事务（避免在持有对象锁时新开连接）；
        否则使用独立事务提交。
        """
        target = session or self._database.session()
        try:
            self._upsert_deletion_intent(
                target,
                user_id,
                request_id,
                object_uri,
                status="DONE" if done else "PENDING",
                bump_attempts=not done,
                error="" if done else (error or "unknown"),
            )
            if session is None:
                self._commit(target)
        finally:
            if session is None:
                target.close()

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
            referenced = _referencing_object_uris(session, user_id, request_id, owned)
        return [uri for uri in owned if uri not in referenced]

    def register_deletion_intents(
        self, user_id: str, request_id: str, object_uris: Sequence[str]
    ) -> None:
        """登记某个运行的持久化对象删除意图。

        同一 ``(user_id, request_id, object_uri)`` 只能有一行，但 ``request_id`` 允许被
        重新拥有（例如评测重跑）：此时该运行重新引用的对象必须回到 PENDING，
        否则一次旧的 DONE 记录会让物理文件永久泄漏。
        """
        if not object_uris:
            return
        with self._database.session() as session:
            # 删除意图引用 users 外键；先确保用户存在（幂等且并发安全）。
            self._ensure_user(session, user_id)
            session.flush()
            known = {
                row[0]: row[1]
                for row in session.execute(
                    select(DeletionIntent.object_uri, DeletionIntent.status).where(
                        DeletionIntent.user_id == user_id,
                        DeletionIntent.request_id == request_id,
                    )
                ).all()
            }
            for uri in sorted(set(object_uris)):
                if uri in known and known[uri] == "PENDING":
                    continue
                # 新登记，或该运行重新拥有此对象（旧的 DONE 必须回到 PENDING）。
                self._upsert_deletion_intent(
                    session,
                    user_id,
                    request_id,
                    uri,
                    status="PENDING",
                    bump_attempts=False,
                    error=INTERNAL_ERROR_PENDING_PHYSICAL_DELETE,
                )
            session.commit()

    def pending_deletion_intents(self, user_id: str, request_id: str) -> list[DeletionIntent]:
        """返回仍未确认删除的意图行（物理删除重试的唯一权威来源）。"""
        with self._database.session() as session:
            return list(
                session.execute(
                    select(DeletionIntent)
                    .where(
                        DeletionIntent.user_id == user_id,
                        DeletionIntent.request_id == request_id,
                        DeletionIntent.status == "PENDING",
                    )
                    .order_by(DeletionIntent.object_uri)
                ).scalars()
            )

    def pending_deletion_uris(self, user_id: str, request_id: str) -> list[str]:
        """返回仍未确认删除的对象地址（跨进程可恢复）。"""
        return [row.object_uri for row in self.pending_deletion_intents(user_id, request_id)]

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
                granularity=_memory_granularity(row.granularity),
                request_id=row.request_id,
                source_position=row.source_position,
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
                statement = statement.where(Memory.keywords.op("?|")(pg_array(list(keywords))))
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
                granularity=_memory_granularity(row.granularity),
                request_id=row.request_id,
                source_position=row.source_position,
            )
            for row in rows
        ]

    def context_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str]
    ) -> list[MemoryCandidate]:
        """获取同用户运行的上下文锚点，供关系扩展使用。"""
        ids = list(dict.fromkeys(request_ids))[:256]
        if not ids:
            return []
        with self._database.session() as session:
            rows = session.execute(
                select(Memory).where(
                    Memory.user_id == user_id,
                    Memory.request_id.in_(ids),
                    Memory.granularity == "context",
                ).order_by(Memory.request_id, Memory.id)
            ).scalars().all()
        return [self._memory_candidate(row) for row in rows]

    def message_candidates_for_requests(
        self, user_id: str, request_ids: Sequence[str], limit: int
    ) -> list[MemoryCandidate]:
        """有界补入同用户运行的消息证据，保持原始消息顺序。"""
        ids = list(dict.fromkeys(request_ids))[:256]
        if not ids or limit < 1:
            return []
        with self._database.session() as session:
            rows = session.execute(
                select(Memory).where(
                    Memory.user_id == user_id,
                    Memory.request_id.in_(ids),
                    Memory.granularity == "message",
                ).order_by(Memory.request_id, Memory.source_position, Memory.id)
                .limit(min(limit, 256))
            ).scalars().all()
        return [self._memory_candidate(row) for row in rows]

    @staticmethod
    def _memory_candidate(row: Memory) -> MemoryCandidate:
        return MemoryCandidate(
            memory_id=row.id, user_id=row.user_id, content=row.summary, score=0.0,
            supersedes=row.supersedes, status=row.status,
            conflict_group_id=row.conflict_group_id, duplicate_of=row.duplicate_of,
            granularity=_memory_granularity(row.granularity), request_id=row.request_id,
            source_position=row.source_position,
        )

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
            pg_insert(User)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
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
