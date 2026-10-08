"""Cloud Smoke 恢复脚本的清单与 CLI 安全边界测试。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import cloud_smoke_recovery as recovery

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "cloud_smoke_recovery.py"


def test_cli_exposes_separate_audit_and_cleanup_commands() -> None:
    """删除入口不能和默认只读审计混成同一个隐式动作。"""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "audit" in result.stdout
    assert "cleanup" in result.stdout


def test_cli_rejects_non_positive_scope_counts() -> None:
    """审计和删除确认的数量都必须是正整数。"""
    with pytest.raises(SystemExit):
        recovery._parse_args(
            [
                "audit",
                "--start",
                "2042-01-01T00:00:00+00:00",
                "--end",
                "2042-01-01T00:01:00+00:00",
                "--manifest",
                "unused.tsv",
                "--expected-count",
                "0",
            ]
        )


def test_manifest_round_trip_is_sorted_unique_and_owner_only(tmp_path: Path) -> None:
    """清理清单必须稳定、无重复，并在落盘后收紧为仅所有者可读写。"""
    path = tmp_path / "smoke.tsv"
    entries = (
        recovery.RunRef("user-b", "request-2"),
        recovery.RunRef("user-a", "request-1"),
    )

    recovery.write_manifest(path, entries)

    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert recovery.read_manifest(path, expected_count=2) == tuple(sorted(entries))


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("user-a\trequest-1\nuser-a\trequest-1\n", "duplicate"),
        ("user-a\n", "two tab-separated fields"),
        ("user-a\trequest-1\n", "expected 2 entries"),
    ],
)
def test_manifest_rejects_ambiguous_or_wrong_scope(
    tmp_path: Path, content: str, expected_error: str
) -> None:
    """重复、格式错误或数量不符都必须在删除前中止。"""
    path = tmp_path / "bad.tsv"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValueError, match=expected_error):
        recovery.read_manifest(path, expected_count=2)


def test_cli_redacts_unexpected_database_errors(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """连接或驱动错误只能暴露异常类别，不能回显连接串中的敏感片段。"""
    secret = "private-database-secret"

    exit_code = recovery.main(
        [
            "audit",
            "--start",
            "2042-01-01T00:00:00+00:00",
            "--end",
            "2042-01-01T00:01:00+00:00",
            "--manifest",
            str(tmp_path / "never-created.tsv"),
            "--expected-count",
            "1",
            "--database-url",
            f"invalid-{secret}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.out
    assert secret not in captured.err
    assert '"complete": false' in captured.out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics are validated on Linux")
def test_manifest_reader_rejects_world_readable_file_and_symlink(tmp_path: Path) -> None:
    """清理阶段只能消费 0600 普通文件，不能跟随可替换的符号链接。"""
    insecure = tmp_path / "insecure.tsv"
    insecure.write_text("user-a\trequest-1\n", encoding="utf-8")
    insecure.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        recovery.read_manifest(insecure, expected_count=1)

    target = tmp_path / "target.tsv"
    target.write_text("user-a\trequest-1\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link.tsv"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        recovery.read_manifest(link, expected_count=1)
