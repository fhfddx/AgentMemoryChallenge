"""Cloud Smoke 恢复脚本的真实数据库边界测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from scripts import cloud_smoke_recovery as recovery
from sqlalchemy import func, select, text, update

from masm.schemas.api import AddRequest
from masm.services.add_service import AddService
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import RequestLedger
from masm.storage.repositories import MemoryRepository


def test_read_only_snapshot_enforces_database_transaction_mode(database: Database) -> None:
    """审计连接必须由 PostgreSQL 强制为只读事务，而不只依赖代码约定。"""
    with recovery.read_only_snapshot(database) as connection:
        assert connection.execute(text("SHOW transaction_read_only")).scalar_one() == "on"


def test_audit_window_selects_committed_runs_and_emits_only_redacted_summary(
    database: Database, add_service: AddService, asset_store: AssetStore
) -> None:
    """时间窗清单来自真实账本，公开报告不得泄露原始用户或请求标识。"""
    suffix = uuid4().hex[:12]
    user_id = f"cloud-audit-user-{suffix}"
    request_id = f"cloud-audit-request-{suffix}"
    add_service.add(
        AddRequest(
            request_id=request_id,
            user_id=user_id,
            session_id="cloud-audit-session",
            messages=[{"role": "user", "content": "audit fixture"}],
        )
    )
    anchor = datetime(2040, 1, 2, 3, 4, 5, tzinfo=UTC)
    with database.session() as session:
        session.execute(
            update(RequestLedger)
            .where(
                RequestLedger.user_id == user_id,
                RequestLedger.request_id == request_id,
            )
            .values(created_at=anchor, updated_at=anchor)
        )
        session.commit()

    try:
        result = recovery.audit_window(
            database,
            start=anchor - timedelta(seconds=1),
            end=anchor + timedelta(seconds=1),
        )

        assert result.entries == (recovery.RunRef(user_id, request_id),)
        assert result.table_counts["request_ledger"] == 1
        assert result.table_counts["source_messages"] == 1
        assert result.table_counts["memories"] >= 2
        assert result.table_counts["assets"] == 0
        serialized = json.dumps(result.public_report(), sort_keys=True)
        assert user_id not in serialized
        assert request_id not in serialized
        assert len(result.groups[0].user_hash) == 12
    finally:
        DeletionService(MemoryRepository(database), asset_store).delete_run(
            request_id, user_id=user_id
        )


def test_cleanup_requires_exact_confirmation_and_deletes_only_manifest_scope(
    tmp_path: Path,
    database: Database,
    add_service: AddService,
    asset_store: AssetStore,
) -> None:
    """数量确认不匹配时零写入；匹配时仅删除清单中的运行。"""
    suffix = uuid4().hex[:12]
    user_id = f"cloud-cleanup-user-{suffix}"
    target_request = f"cloud-cleanup-target-{suffix}"
    keep_request = f"cloud-cleanup-keep-{suffix}"
    for request_id in (target_request, keep_request):
        add_service.add(
            AddRequest(
                request_id=request_id,
                user_id=user_id,
                session_id="cloud-cleanup-session",
                messages=[{"role": "user", "content": f"fixture {request_id}"}],
            )
        )
    manifest = tmp_path / "cleanup.tsv"
    recovery.write_manifest(manifest, (recovery.RunRef(user_id, target_request),))

    with pytest.raises(ValueError, match="confirmation count"):
        recovery.cleanup_manifest(
            database,
            asset_store,
            manifest,
            expected_count=1,
            confirm_delete=2,
        )
    with database.session() as session:
        assert (
            session.execute(
                select(func.count())
                .select_from(RequestLedger)
                .where(RequestLedger.user_id == user_id)
            ).scalar_one()
            == 2
        )

    try:
        result = recovery.cleanup_manifest(
            database,
            asset_store,
            manifest,
            expected_count=1,
            confirm_delete=1,
        )

        assert result.complete is True
        assert result.totals["runs"] == 1
        assert result.remaining == {
            "assets": 0,
            "memories": 0,
            "processing_runs": 0,
            "request_ledger": 0,
            "source_messages": 0,
        }
        serialized = json.dumps(result.public_report(), sort_keys=True)
        assert user_id not in serialized
        assert target_request not in serialized
        with database.session() as session:
            remaining = set(
                session.execute(
                    select(RequestLedger.request_id).where(RequestLedger.user_id == user_id)
                ).scalars()
            )
        assert remaining == {keep_request}
    finally:
        DeletionService(MemoryRepository(database), asset_store).delete_run(
            keep_request, user_id=user_id
        )


def test_cli_audit_then_cleanup_uses_redacted_json_and_exact_manifest(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    database_url: str,
    database: Database,
    add_service: AddService,
    asset_store: AssetStore,
) -> None:
    """运维入口按审计、清理两阶段执行，标准输出始终脱敏。"""
    suffix = uuid4().hex[:12]
    user_id = f"cloud-cli-user-{suffix}"
    request_id = f"cloud-cli-request-{suffix}"
    add_service.add(
        AddRequest(
            request_id=request_id,
            user_id=user_id,
            session_id="cloud-cli-session",
            messages=[{"role": "user", "content": "cli fixture"}],
        )
    )
    anchor = datetime(2041, 2, 3, 4, 5, 6, tzinfo=UTC)
    with database.session() as session:
        session.execute(
            update(RequestLedger)
            .where(
                RequestLedger.user_id == user_id,
                RequestLedger.request_id == request_id,
            )
            .values(created_at=anchor, updated_at=anchor)
        )
        session.commit()
    manifest = tmp_path / "cli.tsv"

    try:
        audit_exit = recovery.main(
            [
                "audit",
                "--start",
                (anchor - timedelta(seconds=1)).isoformat(),
                "--end",
                (anchor + timedelta(seconds=1)).isoformat(),
                "--manifest",
                str(manifest),
                "--expected-count",
                "1",
                "--database-url",
                database_url,
            ]
        )
        audit_output = capsys.readouterr().out
        assert audit_exit == 0
        assert manifest.exists()
        assert user_id not in audit_output
        assert request_id not in audit_output

        cleanup_exit = recovery.main(
            [
                "cleanup",
                "--manifest",
                str(manifest),
                "--expected-count",
                "1",
                "--confirm-delete",
                "1",
                "--database-url",
                database_url,
                "--asset-dir",
                str(asset_store.base_dir),
            ]
        )
        cleanup_output = capsys.readouterr().out
        assert cleanup_exit == 0
        assert '"complete": true' in cleanup_output
        assert user_id not in cleanup_output
        assert request_id not in cleanup_output
    finally:
        DeletionService(MemoryRepository(database), asset_store).delete_run(
            request_id, user_id=user_id
        )
