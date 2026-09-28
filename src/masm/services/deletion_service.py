"""按评测运行删除数据：数据库记录与对象存储同时清理。

作用域始终是「明确用户 + 明确运行（Add request_id）」。删除状态机：

1. **登记意图**：把该运行拥有的对象地址持久化到 ``deletion_intents``（PENDING，幂等），
   共享对象（其他用户/运行仍引用）不登记，因而永不删除物理文件；
2. **删除数据库记录**：运行级资产行、记忆及其 embedding/entity/event、关系、冲突、
   原始消息、处理运行、幂等账本，并清理悬空治理引用与不再被引用的会话；
3. **执行对象删除**：对每个 PENDING 意图尝试删除；成功或确定不存在 → DONE，
   失败 → 累加 attempts 与 last_error，保持 PENDING；
4. **重新读取**：完成与否以数据库中的 PENDING 意图为准，因此第二次调用绝不会在
   文件仍存在时报告 complete=True，且跨进程重启后仍能继续重试。
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
        """删除指定用户该运行的数据库记录，并幂等地重试待清理对象。"""
        registered = self._register(run_id, user_id)
        deleted = self._repo.delete_run(user_id, run_id)

        objects_deleted = 0
        objects_missing = 0
        failed: list[str] = []
        for object_uri in self._repo.pending_deletion_uris(user_id, run_id):
            try:
                removed = self._assets.delete_object(object_uri)
            except (OSError, ValueError) as exc:
                self._repo.mark_deletion_intent(
                    user_id, run_id, object_uri, done=False, error=type(exc).__name__
                )
                failed.append(object_uri)
                continue
            self._repo.mark_deletion_intent(user_id, run_id, object_uri, done=True)
            if removed:
                objects_deleted += 1
            else:
                objects_missing += 1

        remaining = self._repo.pending_deletion_uris(user_id, run_id)
        return DeletionReport(
            user_id=user_id,
            run_id=run_id,
            memories_deleted=deleted.memories,
            assets_deleted=deleted.assets,
            sources_deleted=deleted.sources,
            relations_deleted=deleted.relations,
            conflicts_deleted=deleted.conflicts,
            objects_deleted=objects_deleted,
            objects_missing=objects_missing,
            shared_objects_kept=len(deleted.shared_object_uris),
            retried_uris=tuple(registered),
            failed_object_uris=tuple(remaining),
        )

    def _register(self, run_id: str, user_id: str) -> list[str]:
        """登记该运行独占的对象删除意图；共享对象不登记。"""
        owned = self._repo.deletable_object_uris(user_id, run_id)
        self._repo.register_deletion_intents(user_id, run_id, owned)
        return list(owned)
