"""按评测运行删除数据：数据库记录与对象存储同时清理。

作用域始终是「明确用户 + 明确运行（Add request_id）」；所有数据库读写都在 SQL 阶段按
user_id 过滤，对象路径经过安全解析，禁止目录穿越。删除幂等，部分失败会在报告中显式
暴露，绝不伪装为完全成功。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from masm.storage.assets import AssetStore
from masm.storage.repositories import MemoryRepository


@dataclass(frozen=True)
class DeletionReport:
    """一次运行删除的结果；``complete`` 为 False 表示存在未清理的对象。"""

    user_id: str
    run_id: str
    memories_deleted: int = 0
    assets_deleted: int = 0
    sources_deleted: int = 0
    relations_deleted: int = 0
    conflicts_deleted: int = 0
    objects_deleted: int = 0
    objects_missing: int = 0
    failed_object_uris: Sequence[str] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """所有目标对象是否都已清理。"""
        return not self.failed_object_uris


class DeletionService:
    """按用户 + 运行删除评测数据。"""

    def __init__(self, repository: MemoryRepository, asset_store: AssetStore) -> None:
        self._repo = repository
        self._assets = asset_store

    def delete_run(self, run_id: str, *, user_id: str) -> DeletionReport:
        """删除指定用户在该运行下的全部数据库记录与对象。幂等：重复调用返回零计数。"""
        deleted = self._repo.delete_run(user_id, run_id)
        objects_deleted = 0
        objects_missing = 0
        failed: list[str] = []
        for object_uri in deleted.object_uris:
            try:
                removed = self._assets.delete_object(object_uri)
            except (OSError, ValueError):
                # 路径越界或删除失败：记录为可观察的失败，不中断其余清理。
                failed.append(object_uri)
                continue
            if removed:
                objects_deleted += 1
            else:
                objects_missing += 1
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
            failed_object_uris=tuple(failed),
        )
