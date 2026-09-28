"""按评测运行删除数据：数据库记录与对象存储同时清理。

作用域始终是「明确用户 + 明确运行（Add request_id）」。删除状态机：

1. **删除数据库行并持久化意图**：在单个可回滚事务中删除该运行的记忆、embedding/
   entity/event、关系、冲突、原始消息、处理运行、幂等账本与运行级资产行，并把该运行
   独占（无任何用户/运行再引用）的对象地址登记为 PENDING 删除意图。此阶段绝不触碰
   文件系统，因此事务提交失败时物理对象不会被提前删除。
2. **重试物理删除**：读取持久化的 PENDING 意图（**不依赖已被删除的 SourceMessage**），
   在对象 advisory 锁下重新做权威存活性检查，确认无人引用后才删除对象，并用独立事务
   标记 DONE；失败则累加 attempts 保持 PENDING。
3. **重新读取**：完成与否以数据库中的 PENDING 意图为准，因此第二次调用绝不会在文件仍
   存在时报告 complete=True，且跨进程重启后仍能继续重试。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from masm.storage.assets import AssetStore
from masm.storage.repositories import MemoryRepository


@dataclass(frozen=True)
class DeletionReport:
    """一次运行删除的结果；``complete`` 为 False 表示仍有对象待清理。"""

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
        """所有目标对象是否都已确认删除。"""
        return not self.failed_object_uris


class DeletionService:
    """按用户 + 运行删除评测数据；可重复调用以收敛失败的对象删除。"""

    def __init__(self, repository: MemoryRepository, asset_store: AssetStore) -> None:
        self._repo = repository
        self._assets = asset_store

    def delete_run(self, run_id: str, *, user_id: str) -> DeletionReport:
        """删除指定用户该运行的数据库记录，并重试待清理的物理对象。

        阶段 1（可回滚）：删行 + 登记 PENDING 意图，同一事务提交；
        阶段 2（不可回滚）：在对象锁下重新检查引用后物理删除，独立事务标记 DONE。
        """
        deleted = self._repo.delete_run(user_id, run_id)
        outcome = self._repo.retry_pending_object_deletions(
            user_id, run_id, self._assets.delete_object
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
