"""按评测运行删除数据：数据库记录、正式对象与暂存私有数据同时清理。

作用域始终是「明确用户 + 明确运行（Add request_id）」。删除状态机：

1. **生命周期锁**：整段删除持有运行级 advisory 锁（user_id + request_id），与 Add 的
   「finalize -> publish」互斥，因此删除完成后旧 Add 不能再把暂存文件发布成孤儿对象；
2. **删除数据库行并持久化意图**：在单个可回滚事务中删除该运行的记忆、embedding/
   entity/event、关系、冲突、原始消息、处理运行、幂等账本与运行级资产行，并把
   「独占对象地址」与「该运行的暂存目录」登记为 PENDING 删除意图。此阶段绝不触碰
   文件系统，因此事务提交失败时物理数据不会被提前删除；
3. **重试物理删除**：读取持久化的 PENDING 意图（**不依赖已被删除的 SourceMessage**），
   在对象锁下重新做权威存活性检查，确认无人引用后才删除对象，并用同一持锁连接标记 DONE；
   只有在全部对象意图终结后才清理暂存目录（对象删除失败时暂存仍是重试所需的数据）；
4. **不吞错**：暂存清理失败会累加 attempts 并保持 PENDING，报告 `complete=False`，
   绝不静默假装删除成功。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from masm.storage.assets import AssetStore
from masm.storage.repositories import (
    MemoryRepository,
    lock_run_lifecycle,
    parse_staging_uri,
)


@dataclass(frozen=True)
class DeletionReport:
    """一次运行删除的结果；``complete`` 为 False 表示仍有对象/暂存待清理。"""

    user_id: str
    run_id: str
    memories_deleted: int = 0
    assets_deleted: int = 0
    sources_deleted: int = 0
    relations_deleted: int = 0
    conflicts_deleted: int = 0
    objects_deleted: int = 0
    objects_missing: int = 0
    shared_objects_kept: int = 0
    retried_uris: Sequence[str] = field(default_factory=tuple)
    failed_object_uris: Sequence[str] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """所有目标对象与暂存数据是否都已确认删除。"""
        return not self.failed_object_uris


class DeletionService:
    """按用户 + 运行删除评测数据；可重复调用以收敛失败的对象/暂存清理。"""

    def __init__(self, repository: MemoryRepository, asset_store: AssetStore) -> None:
        self._repo = repository
        self._assets = asset_store

    def delete_run(self, run_id: str, *, user_id: str) -> DeletionReport:
        """删除该运行的数据库记录、正式对象与暂存私有数据（幂等、可重试）。"""
        with self._repo.lifecycle_connection() as lifecycle_connection:
            with lock_run_lifecycle(lifecycle_connection, user_id, run_id):
                return self._delete_locked(run_id, user_id)

    def _delete_locked(self, run_id: str, user_id: str) -> DeletionReport:
        """在生命周期锁内执行两阶段删除。"""
        deleted = self._repo.delete_run(user_id, run_id)

        def _cleanup(staging_uri: str) -> bool:
            owner_token = parse_staging_uri(staging_uri)
            return self._assets.cleanup_staging(user_id, run_id, owner_token)

        outcome = self._repo.retry_pending_object_deletions(
            user_id,
            run_id,
            self._assets.delete_object,
            _cleanup,
        )

        remaining = self._repo.pending_deletion_uris(user_id, run_id)
        return DeletionReport(
            user_id=user_id,
            run_id=run_id,
            memories_deleted=deleted.memories,
            assets_deleted=deleted.assets,
            sources_deleted=deleted.sources,
            relations_deleted=deleted.relations,
            conflicts_deleted=deleted.conflicts,
            objects_deleted=outcome.deleted,
            objects_missing=outcome.missing,
            shared_objects_kept=len(deleted.shared_object_uris),
            retried_uris=tuple(deleted.object_uris),
            failed_object_uris=tuple(remaining),
        )
