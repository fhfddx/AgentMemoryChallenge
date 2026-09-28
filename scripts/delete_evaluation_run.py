"""删除指定评测运行（用户 + Add request_id）的数据库记录与对象文件。

用法::

    python scripts/delete_evaluation_run.py --user-id U --run-id R
"""

import argparse
import sys
from pathlib import Path

from masm.config import Settings
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository

DEFAULT_ASSET_DIR = Path("artifacts/assets")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除指定评测运行的数据")
    parser.add_argument("--user-id", required=True, help="目标用户标识（必填，作用域限定）")
    parser.add_argument("--run-id", required=True, help="目标运行（Add request_id）")
    parser.add_argument("--asset-dir", default=str(DEFAULT_ASSET_DIR), help="对象存储根目录")
    parser.add_argument("--database-url", default=None, help="覆盖数据库连接串")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行删除；存在未清理对象时返回非零退出码。"""
    args = _parse_args(argv)
    settings = Settings.from_env()
    database_url = args.database_url or settings.database_url
    database = Database.create(database_url)
    service = DeletionService(MemoryRepository(database), AssetStore(Path(args.asset_dir)))

    report = service.delete_run(args.run_id, user_id=args.user_id)
    print(
        f"user={report.user_id} run={report.run_id} "
        f"memories={report.memories_deleted} sources={report.sources_deleted} "
        f"assets={report.assets_deleted} objects={report.objects_deleted} "
        f"missing={report.objects_missing} failed={len(report.failed_object_uris)} "
        f"complete={report.complete}"
    )
    if not report.complete:
        for uri in report.failed_object_uris:
            print(f"未清理对象: {uri}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
