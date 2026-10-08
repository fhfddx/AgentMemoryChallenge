"""Audit and precisely clean one bounded v1.1 evaluation window.

This script is intended to be copied into the already-running v1.1 API
container.  It reads database and asset locations from the same environment as
the service and never prints credentials, raw user ids, request ids, payloads,
or object URIs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, select, tuple_

from masm.config import Settings
from masm.services.deletion_service import DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.models import Asset, Memory, ProcessingRun, RequestLedger, SourceMessage
from masm.storage.repositories import MemoryRepository


@dataclass(frozen=True, order=True)
class RunRef:
    """One deletion scope; raw values are written only to the protected manifest."""

    user_id: str
    request_id: str


@dataclass(frozen=True)
class AuditGroup:
    """Redacted ledger summary for one user and status."""

    user_hash: str
    status: str
    requests: int
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class AuditResult:
    """Bounded audit result; raw identifiers live only in ``entries``."""

    start: datetime
    end: datetime
    entries: tuple[RunRef, ...]
    groups: tuple[AuditGroup, ...]
    table_counts: dict[str, int]

    def public_report(self) -> dict[str, Any]:
        """Return a JSON-safe report that excludes raw identifiers."""
        return {
            "mode": "audit",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "manifest_rows": len(self.entries),
            "groups": [
                {
                    "user_hash": group.user_hash,
                    "status": group.status,
                    "requests": group.requests,
                    "first_at": group.first_at.isoformat(),
                    "last_at": group.last_at.isoformat(),
                }
                for group in self.groups
            ],
            "table_counts": dict(sorted(self.table_counts.items())),
        }


@dataclass(frozen=True)
class CleanupResult:
    """Aggregate deletion result without raw identifiers or object locations."""

    totals: dict[str, int]
    remaining: dict[str, int]
    failures: int

    @property
    def complete(self) -> bool:
        return self.failures == 0 and not any(self.remaining.values())

    def public_report(self) -> dict[str, Any]:
        return {
            "mode": "cleanup",
            "complete": self.complete,
            "failures": self.failures,
            "totals": dict(sorted(self.totals.items())),
            "remaining": dict(sorted(self.remaining.items())),
        }


def _validate_ref(ref: RunRef) -> None:
    for name, value in (("user_id", ref.user_id), ("request_id", ref.request_id)):
        if not value or any(marker in value for marker in ("\t", "\r", "\n")):
            raise ValueError(f"{name} must be non-empty and single-line")


def write_manifest(path: Path, entries: tuple[RunRef, ...]) -> None:
    """Create a deterministic owner-only manifest without overwriting evidence."""
    ordered = tuple(sorted(entries))
    for entry in ordered:
        _validate_ref(entry)
    if len(set(ordered)) != len(ordered):
        raise ValueError("manifest contains duplicate entries")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        path.chmod(0o600)
        for entry in ordered:
            stream.write(f"{entry.user_id}\t{entry.request_id}\n")
        stream.flush()


def read_manifest(path: Path, *, expected_count: int) -> tuple[RunRef, ...]:
    """Load a manifest only when its shape, uniqueness, and exact count match."""
    if expected_count < 1:
        raise ValueError("expected_count must be positive")
    if path.is_symlink():
        raise ValueError("manifest must be a regular file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("manifest must be a regular file")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("manifest must be owner-only mode 0600")
        content = stream.read()
    entries: list[RunRef] = []
    for line_number, raw in enumerate(content.splitlines(), start=1):
        fields = raw.split("\t")
        if len(fields) != 2:
            raise ValueError(f"line {line_number} must contain two tab-separated fields")
        entry = RunRef(*fields)
        _validate_ref(entry)
        entries.append(entry)
    if len(entries) != expected_count:
        raise ValueError(f"expected {expected_count} entries, found {len(entries)}")
    if len(set(entries)) != len(entries):
        raise ValueError("manifest contains duplicate entries")
    return tuple(sorted(entries))


def _user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]


@contextmanager
def read_only_snapshot(database: Database) -> Iterator[Connection]:
    """Yield one repeatable-read transaction that PostgreSQL enforces as read-only."""
    with database.engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    ) as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield connection
        finally:
            transaction.rollback()


def audit_window(database: Database, *, start: datetime, end: datetime) -> AuditResult:
    """Read ledger rows in ``[start, end)`` and count only committed run scopes."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("audit boundaries must be timezone-aware")
    if start >= end:
        raise ValueError("audit start must be before end")

    with read_only_snapshot(database) as connection:
        rows = list(
            connection.execute(
                select(
                    RequestLedger.user_id,
                    RequestLedger.request_id,
                    RequestLedger.status,
                    RequestLedger.created_at,
                    RequestLedger.updated_at,
                )
                .where(RequestLedger.created_at >= start, RequestLedger.created_at < end)
                .order_by(RequestLedger.created_at, RequestLedger.user_id, RequestLedger.request_id)
            )
        )
        entries = tuple(
            sorted(RunRef(row.user_id, row.request_id) for row in rows if row.status == "COMMITTED")
        )
        scopes = [(entry.user_id, entry.request_id) for entry in entries]
        models: dict[str, Any] = {
            "request_ledger": RequestLedger,
            "source_messages": SourceMessage,
            "memories": Memory,
            "assets": Asset,
            "processing_runs": ProcessingRun,
        }
        table_counts: dict[str, int] = {}
        for name, model in models.items():
            if not scopes:
                table_counts[name] = 0
                continue
            table_counts[name] = int(
                connection.execute(
                    select(func.count())
                    .select_from(model)
                    .where(tuple_(model.user_id, model.request_id).in_(scopes))
                ).scalar_one()
            )

    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        grouped.setdefault((row.user_id, row.status), []).append(row)
    groups = tuple(
        AuditGroup(
            user_hash=_user_hash(user_id),
            status=status,
            requests=len(group_rows),
            first_at=min(row.created_at for row in group_rows),
            last_at=max(row.updated_at for row in group_rows),
        )
        for (user_id, status), group_rows in sorted(grouped.items())
    )
    return AuditResult(
        start=start,
        end=end,
        entries=entries,
        groups=groups,
        table_counts=table_counts,
    )


def _count_scopes(database: Database, entries: tuple[RunRef, ...]) -> dict[str, int]:
    scopes = [(entry.user_id, entry.request_id) for entry in entries]
    models: dict[str, Any] = {
        "request_ledger": RequestLedger,
        "source_messages": SourceMessage,
        "memories": Memory,
        "assets": Asset,
        "processing_runs": ProcessingRun,
    }
    counts: dict[str, int] = {}
    with database.session() as session:
        for name, model in models.items():
            counts[name] = int(
                session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(tuple_(model.user_id, model.request_id).in_(scopes))
                ).scalar_one()
            )
    return counts


def cleanup_manifest(
    database: Database,
    asset_store: AssetStore,
    path: Path,
    *,
    expected_count: int,
    confirm_delete: int,
) -> CleanupResult:
    """Delete exactly the validated manifest scopes after an exact numeric confirmation."""
    if confirm_delete != expected_count:
        raise ValueError(
            f"confirmation count {confirm_delete} does not match expected {expected_count}"
        )
    entries = read_manifest(path, expected_count=expected_count)
    service = DeletionService(MemoryRepository(database), asset_store)
    totals = {
        "runs": 0,
        "memories": 0,
        "sources": 0,
        "assets": 0,
        "relations": 0,
        "conflicts": 0,
        "objects_deleted": 0,
        "objects_missing": 0,
        "shared_objects_kept": 0,
    }
    failures = 0
    for entry in entries:
        report = service.delete_run(entry.request_id, user_id=entry.user_id)
        totals["runs"] += 1
        totals["memories"] += report.memories_deleted
        totals["sources"] += report.sources_deleted
        totals["assets"] += report.assets_deleted
        totals["relations"] += report.relations_deleted
        totals["conflicts"] += report.conflicts_deleted
        totals["objects_deleted"] += report.objects_deleted
        totals["objects_missing"] += report.objects_missing
        totals["shared_objects_kept"] += report.shared_objects_kept
        failures += int(not report.complete)
    return CleanupResult(
        totals=totals,
        remaining=_count_scopes(database, entries),
        failures=failures,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="read-only audit and manifest creation")
    audit.add_argument("--start", required=True, type=_parse_timestamp)
    audit.add_argument("--end", required=True, type=_parse_timestamp)
    audit.add_argument("--manifest", required=True, type=Path)
    audit.add_argument("--expected-count", required=True, type=_positive_int)
    audit.add_argument("--database-url", default=None)

    cleanup = commands.add_parser("cleanup", help="delete only entries from a validated manifest")
    cleanup.add_argument("--manifest", required=True, type=Path)
    cleanup.add_argument("--expected-count", required=True, type=_positive_int)
    cleanup.add_argument("--confirm-delete", required=True, type=_positive_int)
    cleanup.add_argument("--database-url", default=None)
    cleanup.add_argument("--asset-dir", default=None, type=Path)
    return parser.parse_args(argv)


def _parse_timestamp(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def _database(database_url: str | None, settings: Settings) -> Database:
    resolved = database_url or settings.database_url
    if not resolved:
        raise ValueError("database URL is required")
    return Database.create(resolved)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)


def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    database = _database(args.database_url, settings)
    if args.command == "audit":
        audit_result = audit_window(database, start=args.start, end=args.end)
        if len(audit_result.entries) != args.expected_count:
            _emit(
                {
                    "mode": "audit",
                    "complete": False,
                    "error": "manifest_count_mismatch",
                    "expected_count": args.expected_count,
                    "actual_count": len(audit_result.entries),
                }
            )
            return 2
        write_manifest(args.manifest, audit_result.entries)
        payload = audit_result.public_report()
        payload.update(
            {
                "complete": True,
                "manifest_mode": f"{args.manifest.stat().st_mode & 0o777:03o}",
                "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            }
        )
        _emit(payload)
        return 0

    asset_dir = args.asset_dir or settings.asset_dir
    cleanup_result = cleanup_manifest(
        database,
        AssetStore(asset_dir),
        args.manifest,
        expected_count=args.expected_count,
        confirm_delete=args.confirm_delete,
    )
    _emit(cleanup_result.public_report())
    return 0 if cleanup_result.complete else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError) as exc:
        _emit(
            {
                "mode": args.command,
                "complete": False,
                "error": type(exc).__name__,
            }
        )
        return 2
    except Exception as exc:
        _emit(
            {
                "mode": args.command,
                "complete": False,
                "error": type(exc).__name__,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
