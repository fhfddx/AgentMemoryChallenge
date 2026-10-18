"""Verify a deployed evidence-graph candidate and optionally run its stability gate.

The default mode is read-only and makes no Provider calls. Interactive invocations report
the real gate status in output while returning zero so ``set -e`` cannot close the parent
shell; ``--strict-exit`` opts into machine-readable nonzero process status. ``--stability``
is explicit, runs only after every verification passes, and suppresses child-process stderr
so Provider bodies or other private diagnostics cannot be rendered by this wrapper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, TextIO, cast

_PROVIDER_VARIABLES = (
    "MASM_LLM_BASE_URL",
    "MASM_LLM_API_KEY",
    "MASM_EMBEDDING_BASE_URL",
    "MASM_EMBEDDING_API_KEY",
)
_INTEGER_SETTING_NAMES = (
    "MASM_MAX_IMAGE_BYTES",
    "MASM_MAX_ADD_IMAGE_BYTES",
    "MASM_MAX_SEARCH_RESPONSE_BYTES",
    "MASM_MAX_TOP_K",
    "MASM_MAX_CONCURRENT_REQUESTS",
    "MASM_MAX_REQUESTS_PER_MINUTE",
    "MASM_EMBEDDING_DIMENSIONS",
    "MASM_MODEL_MAX_ATTEMPTS",
    "MASM_SELECTOR_MAX_CANDIDATES",
    "MASM_SELECTOR_MAX_SELECTED",
    "MASM_SELECTOR_MAX_CHARS_PER_CANDIDATE",
    "MASM_EVIDENCE_GRAPH_MAX_REQUIREMENTS",
    "MASM_EVIDENCE_GRAPH_MAX_SUPPORTS",
    "MASM_EVIDENCE_GRAPH_MAX_LINKS",
)
_FLOAT_SETTING_NAMES = (
    "MASM_MODEL_TIMEOUT_SECONDS",
    "MASM_MIN_TEXT_SIMILARITY",
    "MASM_MIN_IMAGE_SIMILARITY",
    "MASM_MIN_LEXICAL_RANK",
    "MASM_SELECTOR_TIMEOUT_SECONDS",
    "MASM_EVIDENCE_GRAPH_TIMEOUT_SECONDS",
)
_VALIDATION_ISSUE_BY_MESSAGE = {
    "Embedding 维度必须为正整数": "configuration:embedding_dimensions_out_of_range",
    "模型超时必须为正数": "configuration:model_timeout_out_of_range",
    "模型尝试次数只能为 1 或 2": "configuration:model_max_attempts_out_of_range",
    "请求并发上限必须为正整数": "configuration:max_concurrent_requests_out_of_range",
    "每分钟请求上限必须为正整数": "configuration:max_requests_per_minute_out_of_range",
    "文本相似度阈值必须是 [-1, 1] 内的有限值": (
        "configuration:min_text_similarity_out_of_range"
    ),
    "图片相似度阈值必须是 [-1, 1] 内的有限值": (
        "configuration:min_image_similarity_out_of_range"
    ),
    "全文排名阈值必须是 (0, 1] 内的有限值": (
        "configuration:min_lexical_rank_out_of_range"
    ),
    "证据筛选候选上限必须在 1..32 内": (
        "configuration:selector_max_candidates_out_of_range"
    ),
    "证据返回上限必须在 1..12 内且不大于候选上限": (
        "configuration:selector_max_selected_out_of_range"
    ),
    "证据候选文字上限必须在 1..4096 内": (
        "configuration:selector_max_chars_per_candidate_out_of_range"
    ),
    "证据筛选超时必须为正数且不超过模型超时": (
        "configuration:selector_timeout_out_of_range"
    ),
    "证据图需求上限必须在 1..4 内": (
        "configuration:evidence_graph_max_requirements_out_of_range"
    ),
    "证据图支持上限必须在 1..24 内": (
        "configuration:evidence_graph_max_supports_out_of_range"
    ),
    "证据图链接上限必须在 1..24 内": (
        "configuration:evidence_graph_max_links_out_of_range"
    ),
    "证据图超时必须为正数且不超过模型超时": (
        "configuration:evidence_graph_timeout_out_of_range"
    ),
    "证据图模式要求 official-masm 档位": (
        "configuration:evidence_graph_requires_official_masm"
    ),
    "证据图模式要求启用证据筛选回退": (
        "configuration:evidence_graph_requires_selector"
    ),
    "证据图模式要求启用来源上下文": (
        "configuration:evidence_graph_requires_source_context"
    ),
    "正式档位模型配置不完整": (
        "configuration:official_provider_configuration_incomplete"
    ),
    "正式档位 LLM 必须为 gpt-4o-mini": "configuration:llm_model_mismatch",
    "正式档位 Embedding 必须为 text-embedding-v4": (
        "configuration:embedding_model_mismatch"
    ),
    "正式档位 Embedding 维度必须为 1024": (
        "configuration:official_embedding_dimensions_mismatch"
    ),
    "正式档位 LLM Base URL 必须使用 HTTPS": (
        "configuration:llm_base_url_not_https"
    ),
    "正式档位 Embedding Base URL 必须使用 HTTPS": (
        "configuration:embedding_base_url_not_https"
    ),
}
_VALIDATION_SETTING_ISSUES = frozenset(_VALIDATION_ISSUE_BY_MESSAGE.values()) | {
    "configuration:parse_error",
    "configuration:validation_error",
    "configuration:invalid",
}
_ALLOWED_RUNTIME_SETTING_ISSUES = frozenset(
    {f"{name}:malformed_integer" for name in _INTEGER_SETTING_NAMES}
    | {f"{name}:malformed_float" for name in _FLOAT_SETTING_NAMES}
    | {"MASM_RUNTIME_PROFILE:invalid_choice"}
    | _VALIDATION_SETTING_ISSUES
)
_ALLOWED_DIAGNOSTIC_FAILURE_CATEGORIES = frozenset(
    {"none", "planner_unavailable", "planner_invalid", "internal_error", "probe_error"}
)
_ALLOWED_DIAGNOSTIC_STATES = frozenset(
    {"sufficient", "partial", "insufficient", "unknown"}
)
_ALLOWED_DIAGNOSTIC_FAILURE_REASONS = frozenset(
    {
        "passed",
        "graph_failure",
        "state_mismatch",
        "required_evidence_missing",
        "forbidden_evidence_selected",
    }
)
_ALLOWED_DIAGNOSTIC_STATE_TRANSITIONS = frozenset(
    f"{expected}->{actual}"
    for expected in ("sufficient", "partial", "insufficient")
    for actual in _ALLOWED_DIAGNOSTIC_STATES
)
# 结构失败原因是固定枚举，只用于诊断定位。
_ALLOWED_DIAGNOSTIC_PLANNER_FAILURE_REASONS = frozenset(
    {
        "none",
        "schema_invalid",
        "planner_unavailable",
        "internal_error",
        "candidate_scope_mismatch",
        "empty_requirements",
        "requirement_count_exceeded",
        "requirement_quote_not_grounded",
        "requirement_quote_reordered",
        "requirement_quote_paraphrased",
        "support_count_exceeded",
        "support_index_out_of_range",
        "link_count_exceeded",
        "link_index_out_of_range",
        "link_self_reference",
    }
)
_ALLOWED_PROVIDER_CHECK_CATEGORIES = frozenset(
    {
        "success",
        "http_status",
        "timeout",
        "transport",
        "invalid_response",
        "unavailable_unknown",
        "internal_error",
    }
)
_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_CONTAINER_CHECK_TEMPLATE = r"""
import hashlib
import json
import os
from pathlib import Path

import masm

root = Path(masm.__file__).resolve().parent
manifest = PACKAGE_MANIFEST_JSON
manifest_set = set(manifest)
actual_files = set()
package_manifest = "match"
source_digest = None
try:
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("symlink")
        if path.is_file() and "__pycache__" not in relative.parts and path.suffix != ".pyc":
            actual_files.add(relative.as_posix())
    if actual_files != manifest_set:
        package_manifest = "mismatch"
    else:
        digest = hashlib.sha256()
        for relative_text in manifest:
            relative = Path(*relative_text.split("/"))
            path = root / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("invalid manifest file")
            digest.update(relative_text.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        source_digest = digest.hexdigest()
except (OSError, ValueError):
    package_manifest = "mismatch"

try:
    from masm.retrieval.evidence_graph import EvidenceGraphPlanner
    from masm.retrieval.evidence_graph_assembly import assemble
    from masm.retrieval.evidence_graph_rules import validate_proposal
except Exception:
    candidate_modules = "missing"
else:
    candidate_modules = "present"

integer_setting_names = INTEGER_SETTING_NAMES_JSON
float_setting_names = FLOAT_SETTING_NAMES_JSON
runtime_setting_issues = []
for name in integer_setting_names:
    raw = os.environ.get(name)
    if raw is not None and raw != "":
        try:
            int(raw)
        except (TypeError, ValueError):
            runtime_setting_issues.append(f"{name}:malformed_integer")
for name in float_setting_names:
    raw = os.environ.get(name)
    if raw is not None and raw != "":
        try:
            float(raw)
        except (TypeError, ValueError):
            runtime_setting_issues.append(f"{name}:malformed_float")

runtime_profile_value = os.environ.get("MASM_RUNTIME_PROFILE", "local-fake")
if runtime_profile_value not in {"local-fake", "official-baseline", "official-masm"}:
    runtime_setting_issues.append("MASM_RUNTIME_PROFILE:invalid_choice")

validation_issue_by_message = VALIDATION_ISSUE_BY_MESSAGE_JSON

if runtime_setting_issues:
    runtime_settings = "invalid"
else:
    try:
        from masm.config import Settings

        settings = Settings.from_env()
    except Exception:
        runtime_settings = "invalid"
        runtime_setting_issues.append("configuration:parse_error")
    else:
        try:
            settings.validate_runtime()
        except ValueError as exc:
            runtime_settings = "invalid"
            runtime_setting_issues.append(
                validation_issue_by_message.get(
                    str(exc),
                    "configuration:validation_error",
                )
            )
        except Exception:
            runtime_settings = "invalid"
            runtime_setting_issues.append("configuration:validation_error")
        else:
            runtime_settings = "valid"

raw_graph_flag = os.environ.get("MASM_EVIDENCE_GRAPH_ENABLED")
if raw_graph_flag is None:
    graph_flag = "unset(default_off)"
elif raw_graph_flag == "1":
    graph_flag = "enabled"
else:
    graph_flag = "disabled"

provider_names = (
    "MASM_LLM_BASE_URL",
    "MASM_LLM_API_KEY",
    "MASM_EMBEDDING_BASE_URL",
    "MASM_EMBEDDING_API_KEY",
)
print(json.dumps({
    "candidate_modules": candidate_modules,
    "graph_flag": graph_flag,
    "package_manifest": package_manifest,
    "providers": {
        name: "set" if os.environ.get(name) else "missing"
        for name in provider_names
    },
    "runtime_profile": (
        "official-masm"
        if os.environ.get("MASM_RUNTIME_PROFILE") == "official-masm"
        else "not_official_masm"
    ),
    "runtime_setting_issues": runtime_setting_issues,
    "runtime_settings": runtime_settings,
    "source_digest": source_digest,
}, sort_keys=True))
"""


@dataclass(frozen=True)
class CandidateExpectations:
    probe_blob: str
    probe_sha256: str
    source_digest: str


DEFAULT_EXPECTATIONS = CandidateExpectations(
    probe_blob="140887a17c98712ea4478500e02bafc8c3b6677b",
    probe_sha256="07d7d00afe33fbea27495d866c81d866fcca7a3292741313894fd1579458e560",
    source_digest="4e23158f97d8a128a7474698c57bdf2a19d319a2d6aa78b7ee8be78dbeab9fa3",
)


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | bytes | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...


def _run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | bytes | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    if isinstance(input_text, bytes):
        return subprocess.run(
            list(args),
            cwd=cwd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return subprocess.run(
        list(args),
        cwd=cwd,
        input=input_text,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _tracked_package_files(
    result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None,
) -> tuple[str, ...] | None:
    if result is None or result.returncode != 0:
        return None
    stdout = _stdout_text(result)
    if stdout is None:
        return None
    relative_files: list[str] = []
    for entry in stdout.split("\0"):
        if not entry:
            continue
        path = PurePosixPath(entry)
        if path.is_absolute() or path.parts[:2] != ("src", "masm") or len(path.parts) < 3:
            return None
        relative = PurePosixPath(*path.parts[2:])
        if ".." in relative.parts or ".env" in relative.parts:
            return None
        relative_files.append(relative.as_posix())
    return tuple(sorted(relative_files)) if relative_files else None


def _package_digest(root: Path, manifest: Sequence[str]) -> tuple[bool, str | None]:
    try:
        actual_files: set[str] = set()
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                return False, None
            if path.is_file() and "__pycache__" not in relative.parts and path.suffix != ".pyc":
                actual_files.add(relative.as_posix())
        if actual_files != set(manifest):
            return False, None
        digest = hashlib.sha256()
        for relative_text in manifest:
            path = root.joinpath(*PurePosixPath(relative_text).parts)
            if path.is_symlink() or not path.is_file():
                return False, None
            digest.update(relative_text.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return False, None
    return True, digest.hexdigest()


def _git_package_digest(
    repository: Path,
    manifest: Sequence[str],
    runner: CommandRunner,
) -> str | None:
    digest = hashlib.sha256()
    for relative_text in manifest:
        result = _command(
            runner,
            ("git", "cat-file", "blob", f"HEAD:src/masm/{relative_text}"),
            repository=repository,
            input_text=b"",
        )
        if result is None or result.returncode != 0 or not isinstance(result.stdout, bytes):
            return None
        digest.update(relative_text.encode())
        digest.update(b"\0")
        digest.update(result.stdout)
        digest.update(b"\0")
    return digest.hexdigest()


def _worktree_matches_head(
    repository: Path,
    root: Path,
    manifest: Sequence[str],
    runner: CommandRunner,
) -> bool:
    for relative_text in manifest:
        path = root.joinpath(*PurePosixPath(relative_text).parts)
        try:
            contents = path.read_bytes()
        except OSError:
            return False
        worktree_blob = _clean_line(
            _command(
                runner,
                (
                    "git",
                    "hash-object",
                    f"--path=src/masm/{relative_text}",
                    "--stdin",
                ),
                repository=repository,
                input_text=contents,
            )
        )
        committed_blob = _clean_line(
            _command(
                runner,
                ("git", "rev-parse", f"HEAD:src/masm/{relative_text}"),
                repository=repository,
            )
        )
        if worktree_blob is None or worktree_blob != committed_blob:
            return False
    return True


def _probe_bytes_match(probe_bytes: bytes, expected_sha256: str) -> bool:
    """比对将要送进容器的 probe 字节；行尾归一化后比较，使结论与平台无关。

    ``probe_blob`` 已经独立钉住已提交内容，因此这里的 CRLF/LF 差异不改变被审计的程序。
    """
    normalized = probe_bytes.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest() == expected_sha256


def _container_check(manifest: Sequence[str]) -> str:
    return (
        _CONTAINER_CHECK_TEMPLATE.replace(
            "PACKAGE_MANIFEST_JSON",
            json.dumps(list(manifest)),
        )
        .replace(
            "INTEGER_SETTING_NAMES_JSON",
            json.dumps(_INTEGER_SETTING_NAMES),
        )
        .replace(
            "FLOAT_SETTING_NAMES_JSON",
            json.dumps(_FLOAT_SETTING_NAMES),
        )
        .replace(
            "VALIDATION_ISSUE_BY_MESSAGE_JSON",
            json.dumps(_VALIDATION_ISSUE_BY_MESSAGE),
        )
    )


def _open_result_file(repository: Path, requested: Path | None) -> TextIO | None:
    try:
        repository_root = repository.resolve(strict=True)
        if requested is None:
            temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
            if temp_root.is_relative_to(repository_root):
                return None
            descriptor, _ = tempfile.mkstemp(
                prefix="masm-evidence-graph-stability-",
                suffix=".jsonl",
                dir=temp_root,
                text=True,
            )
            return _open_descriptor(descriptor)

        unresolved = requested if requested.is_absolute() else repository / requested
        resolved = unresolved.parent.resolve(strict=True) / unresolved.name
        if resolved.is_relative_to(repository_root):
            return None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags, 0o600)
        return _open_descriptor(descriptor)
    except OSError:
        return None


def _open_descriptor(descriptor: int) -> TextIO | None:
    try:
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        return None


def _close_result_file(result_file: TextIO) -> bool:
    try:
        result_file.close()
    except (OSError, ValueError):
        return False
    return True


def _write_result_file(result_file: TextIO, rendered: str) -> bool:
    write_succeeded = True
    try:
        result_file.write(rendered)
        result_file.flush()
    except (OSError, UnicodeError, ValueError):
        write_succeeded = False
    return _close_result_file(result_file) and write_succeeded


def _command(
    runner: CommandRunner,
    args: Sequence[str],
    *,
    repository: Path,
    input_text: str | bytes | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None:
    try:
        return runner(args, cwd=repository, input_text=input_text)
    except OSError:
        return None


def _stdout_text(
    result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
) -> str | None:
    if isinstance(result.stdout, bytes):
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            return None
    return result.stdout


def _clean_line(
    result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None,
) -> str | None:
    if result is None or result.returncode != 0:
        return None
    stdout = _stdout_text(result)
    if stdout is None:
        return None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _container_payload(
    result: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None,
) -> Mapping[str, object] | None:
    rendered = _clean_line(result)
    if rendered is None:
        return None
    try:
        payload = json.loads(rendered)
    except (TypeError, ValueError):
        return None
    return cast(Mapping[str, object], payload) if isinstance(payload, dict) else None


def _verify_candidate(
    *,
    repository: Path,
    container: str,
    output: TextIO,
    error: TextIO,
    runner: CommandRunner,
    expectations: CandidateExpectations,
) -> tuple[int, bytes | None]:
    probe_path = repository / "scripts" / "evidence_graph_probe.py"
    package_root = repository / "src" / "masm"
    if not probe_path.is_file() or not package_root.is_dir():
        print("candidate_checkout=invalid", file=error)
        return 2, None
    try:
        probe_bytes = probe_path.read_bytes()
    except OSError:
        print("probe_worktree=unreadable", file=error)
        return 2, None

    probe_blob = _clean_line(
        _command(
            runner,
            ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"),
            repository=repository,
        )
    )
    manifest = _tracked_package_files(
        _command(
            runner,
            ("git", "ls-files", "-z", "--", "src/masm"),
            repository=repository,
        )
    )
    if manifest is None:
        print("source_manifest=invalid", file=error)
        return 2, None
    host_manifest_matches, host_digest = _package_digest(package_root, manifest)
    committed_digest = _git_package_digest(repository, manifest, runner)
    worktree_matches_head = host_digest == committed_digest or (
        host_manifest_matches
        and _worktree_matches_head(repository, package_root, manifest, runner)
    )
    status = _command(
        runner,
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        repository=repository,
    )
    image = _clean_line(
        _command(
            runner,
            ("docker", "inspect", "--format", "{{.Image}}", container),
            repository=repository,
        )
    )
    container_result = _command(
        runner,
        ("docker", "exec", "-i", container, "python", "-"),
        repository=repository,
        input_text=_container_check(manifest),
    )
    payload = _container_payload(container_result)
    if status is None or status.returncode != 0 or image is None:
        print("verification_error=command_failed", file=error)
        return 2, None
    if payload is None:
        print("container_check=failed", file=error)
        return 2, None

    status_stdout = _stdout_text(status)
    if status_stdout is None:
        print("verification_error=invalid_output", file=error)
        return 2, None
    checkout_clean = not status_stdout.strip()
    probe_matches = probe_blob == expectations.probe_blob
    probe_worktree_matches = _probe_bytes_match(probe_bytes, expectations.probe_sha256)
    container_manifest_matches = payload.get("package_manifest") == "match"
    source_manifest_matches = host_manifest_matches and container_manifest_matches
    container_digest = payload.get("source_digest")
    source_matches = (
        source_manifest_matches
        and worktree_matches_head
        and committed_digest == expectations.source_digest
        and container_digest == expectations.source_digest
    )
    runtime_profile = payload.get("runtime_profile")
    runtime_settings = payload.get("runtime_settings")
    raw_runtime_setting_issues = payload.get("runtime_setting_issues")
    safe_runtime_setting_issues = []
    if isinstance(raw_runtime_setting_issues, list):
        safe_runtime_setting_issues = list(
            dict.fromkeys(
                issue
                for issue in raw_runtime_setting_issues
                if isinstance(issue, str) and issue in _ALLOWED_RUNTIME_SETTING_ISSUES
            )
        )
    if runtime_settings == "valid":
        safe_runtime_setting_issues = []
    elif not safe_runtime_setting_issues:
        safe_runtime_setting_issues = ["configuration:invalid"]
    candidate_modules = payload.get("candidate_modules")
    graph_flag = payload.get("graph_flag")
    providers = payload.get("providers")
    provider_statuses = providers if isinstance(providers, dict) else {}

    print("repository_revision=present", file=output)
    print("container_image=present", file=output)
    print(f"checkout_clean={'yes' if checkout_clean else 'no'}", file=output)
    print(f"probe_blob_match={'yes' if probe_matches else 'no'}", file=output)
    print(
        f"probe_worktree_match={'yes' if probe_worktree_matches else 'no'}",
        file=output,
    )
    print(
        f"source_manifest_match={'yes' if source_manifest_matches else 'no'}",
        file=output,
    )
    print(f"source_match={'yes' if source_matches else 'no'}", file=output)
    print(f"MASM_RUNTIME_PROFILE={runtime_profile}", file=output)
    safe_runtime_settings = (
        runtime_settings if runtime_settings in {"valid", "invalid"} else "invalid"
    )
    print(
        f"runtime_settings={safe_runtime_settings}",
        file=output,
    )
    for issue in safe_runtime_setting_issues:
        print(f"runtime_setting_issue={issue}", file=output)
    for name in _PROVIDER_VARIABLES:
        value = provider_statuses.get(name)
        print(f"{name}={value if value in {'set', 'missing'} else 'invalid'}", file=output)
    print(f"candidate_modules={candidate_modules}", file=output)
    print(f"graph_flag={graph_flag}", file=output)

    if (
        not checkout_clean
        or not probe_matches
        or not probe_worktree_matches
        or not source_manifest_matches
        or not source_matches
    ):
        return 3, None
    if (
        runtime_profile != "official-masm"
        or runtime_settings != "valid"
        or candidate_modules != "present"
        or any(provider_statuses.get(name) != "set" for name in _PROVIDER_VARIABLES)
    ):
        return 2, None
    return 0, probe_bytes


# 运行前预注册的修正判据（交接文档第 22 节）：planner 已经是 temperature=0.0，但 Provider 侧仍存在
# 不可消除的抖动，且有两个负例的期望状态取决于模型判断。判据因此改为：逐例（两个豁免用例除外）
# 五轮中至少四轮通过、每轮至少（用例总数 − 豁免数）通过，并且任何一轮都不得出现
# forbidden_evidence_selected。每轮下限由数据推导，避免与豁免数量自相矛盾。
_AMENDED_MIN_ROUNDS_PER_CASE = 4
_AMENDED_EXEMPT_CASES = frozenset({"missing_value_negative", "chinese_context_negative"})


def _amended_stability_criterion(rendered: str) -> Mapping[str, object] | None:
    """按预注册判据评估五轮稳定性输出；任何无法安全解析的输入都返回 None。"""
    rounds: list[dict[str, bool]] = []
    round_summaries: list[Mapping[str, object]] = []
    current: dict[str, bool] = {}
    forbidden_total = 0
    round_results = 0
    for line in rendered.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(row, dict):
            return None
        name = row.get("case")
        if name == "stability_summary":
            continue
        if name == "round_result":
            round_results += 1
            continue
        if name == "summary":
            reasons = row.get("failure_reasons")
            if not isinstance(reasons, dict):
                return None
            for key, value in reasons.items():
                if (
                    key not in _ALLOWED_DIAGNOSTIC_FAILURE_REASONS
                    or type(value) is not int
                    or value < 1
                ):
                    return None
            forbidden_total += int(reasons.get("forbidden_evidence_selected", 0))
            rounds.append(dict(current))
            round_summaries.append(row)
            current = {}
            continue
        if not isinstance(name, str) or not name:
            return None
        passed = row.get("passed")
        if not isinstance(passed, bool):
            return None
        current[name] = passed
    if current or not rounds or round_results != len(rounds):
        return None

    per_case: dict[str, int] = {}
    for round_cases in rounds:
        for case_name, passed in round_cases.items():
            per_case[case_name] = per_case.get(case_name, 0) + int(passed)
    # 每轮下限 = 用例总数 − 豁免用例数：豁免只应放宽"逐例"要求，不能同时吃掉每轮的余量。
    # 硬编码常量会与豁免数量自相矛盾（26 例豁免 2 个时下限必须是 24，见交接文档第 23.3、29 节）。
    exempt_present = len(_AMENDED_EXEMPT_CASES & per_case.keys())
    minimum_per_round = len(per_case) - exempt_present
    rounds_meeting = 0
    for row in round_summaries:
        positive = row.get("positive_passed")
        negative = row.get("negative_passed")
        if type(positive) is not int or type(negative) is not int:
            return None
        if positive + negative >= minimum_per_round:
            rounds_meeting += 1
    graded = sorted(name for name in per_case if name not in _AMENDED_EXEMPT_CASES)
    if not graded:
        return None
    cases_meeting = sum(
        1 for name in graded if per_case[name] >= _AMENDED_MIN_ROUNDS_PER_CASE
    )
    eligible = (
        len(rounds) == 5
        and rounds_meeting == len(rounds)
        and cases_meeting == len(graded)
        and forbidden_total == 0
    )
    return {
        "case": "amended_criterion",
        "candidate_eligible": eligible,
        "cases_meeting_minimum": cases_meeting,
        "cases_total": len(graded),
        "exempt_cases": exempt_present,
        "forbidden_evidence_total": forbidden_total,
        "minimum_per_round": minimum_per_round,
        "round_count": len(rounds),
        "rounds_meeting_minimum": rounds_meeting,
    }


def _run_stability(
    *,
    repository: Path,
    container: str,
    result_path: Path | None,
    output: TextIO,
    error: TextIO,
    runner: CommandRunner,
    probe_bytes: bytes,
    criterion: str = "frozen",
) -> int:
    result_file = _open_result_file(repository, result_path)
    if result_file is None:
        print("stability_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    result = _command(
        runner,
        ("docker", "exec", "-i", container, "python", "-", "--stability"),
        repository=repository,
        input_text=probe_bytes,
    )
    if result is None:
        _close_result_file(result_file)
        print("stability_exec=failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    rendered = _stdout_text(result)
    if rendered is None:
        _close_result_file(result_file)
        print("stability_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    if not _write_result_file(result_file, rendered):
        print("stability_output=write_failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2

    lines = [line for line in rendered.splitlines() if line.strip()]
    summary: Mapping[str, object] | None = None
    if lines:
        try:
            candidate = json.loads(lines[-1])
        except (TypeError, ValueError):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("case") == "stability_summary":
            summary = cast(Mapping[str, object], candidate)
    if summary is None:
        print("stability_summary=missing", file=error)
    else:
        allowed = {"case", "round_count", "rounds_passed", "candidate_eligible"}
        sanitized = {key: value for key, value in summary.items() if key in allowed}
        print(json.dumps(sanitized, sort_keys=True), file=output)
    print(f"probe_exit_code={result.returncode}", file=output)
    if summary is None:
        return 2
    if criterion != "amended":
        return result.returncode
    verdict = _amended_stability_criterion(rendered)
    if verdict is None:
        print("amended_criterion=invalid", file=error)
        print("amended_candidate_eligible=false", file=output)
        return 2
    print("criterion=amended", file=output)
    print(json.dumps(verdict, sort_keys=True), file=output)
    eligible = verdict.get("candidate_eligible") is True
    print(f"amended_candidate_eligible={'true' if eligible else 'false'}", file=output)
    return 0 if eligible else 1


def _diagnostic_summary(rendered: str) -> Mapping[str, object] | None:
    summary: Mapping[str, object] | None = None
    case_count = 0
    failure_categories: dict[str, int] = {}
    evidence_states: dict[str, int] = {}
    for line in rendered.splitlines():
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(candidate, dict):
            return None
        if candidate.get("case") == "summary":
            if summary is not None:
                return None
            summary = cast(Mapping[str, object], candidate)
            continue
        case_count += 1
        if case_count > 64:
            return None
        category = candidate.get("failure_category")
        if category not in _ALLOWED_DIAGNOSTIC_FAILURE_CATEGORIES:
            return None
        state = candidate.get("evidence_state", "unknown")
        if state not in _ALLOWED_DIAGNOSTIC_STATES:
            return None
        assert isinstance(category, str)
        assert isinstance(state, str)
        failure_categories[category] = failure_categories.get(category, 0) + 1
        evidence_states[state] = evidence_states.get(state, 0) + 1

    if summary is None:
        return None
    integer_fields = (
        "case_count",
        "positive_total",
        "positive_passed",
        "negative_total",
        "negative_passed",
        "fallbacks",
        "failure_count",
    )
    safe_integers: dict[str, int] = {}
    for name in integer_fields:
        value = summary.get(name)
        if type(value) is not int or value < 0:
            return None
        safe_integers[name] = value
    eligible = summary.get("candidate_eligible")
    if not isinstance(eligible, bool) or safe_integers["case_count"] != case_count:
        return None
    safe_maps: dict[str, dict[str, int]] = {}
    for name, allowed_keys in (
        ("failure_reasons", _ALLOWED_DIAGNOSTIC_FAILURE_REASONS),
        ("state_transitions", _ALLOWED_DIAGNOSTIC_STATE_TRANSITIONS),
        ("planner_failure_reasons", _ALLOWED_DIAGNOSTIC_PLANNER_FAILURE_REASONS),
    ):
        raw = summary.get(name)
        if not isinstance(raw, dict):
            return None
        sanitized: dict[str, int] = {}
        for key, value in raw.items():
            if key not in allowed_keys or type(value) is not int or value < 1:
                return None
            assert isinstance(key, str)
            sanitized[key] = value
        if sum(sanitized.values()) != case_count:
            return None
        safe_maps[name] = dict(sorted(sanitized.items()))
    return {
        "case": "diagnostic_summary",
        **safe_integers,
        "candidate_eligible": eligible,
        "failure_categories": dict(sorted(failure_categories.items())),
        "evidence_states": dict(sorted(evidence_states.items())),
        **safe_maps,
    }


def _run_diagnostic(
    *,
    repository: Path,
    container: str,
    result_path: Path | None,
    output: TextIO,
    error: TextIO,
    runner: CommandRunner,
    probe_bytes: bytes,
) -> int:
    result_file = _open_result_file(repository, result_path)
    if result_file is None:
        print("diagnostic_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    result = _command(
        runner,
        ("docker", "exec", "-i", container, "python", "-"),
        repository=repository,
        input_text=probe_bytes,
    )
    if result is None:
        _close_result_file(result_file)
        print("diagnostic_exec=failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    rendered = _stdout_text(result)
    if rendered is None:
        _close_result_file(result_file)
        print("diagnostic_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    if not _write_result_file(result_file, rendered):
        print("diagnostic_output=write_failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    summary = _diagnostic_summary(rendered)
    if summary is None:
        print("diagnostic_summary=missing", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    print(json.dumps(summary, sort_keys=True), file=output)
    print(f"probe_exit_code={result.returncode}", file=output)
    return result.returncode


def _provider_check_payload(rendered: str) -> Mapping[str, object] | None:
    lines = [line for line in rendered.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        candidate = json.loads(lines[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(candidate, dict) or candidate.get("case") != "provider_check":
        return None
    category = candidate.get("category")
    if category not in _ALLOWED_PROVIDER_CHECK_CATEGORIES:
        return None
    safe: dict[str, object] = {"case": "provider_check", "category": category}
    attempt = candidate.get("attempt")
    if attempt is not None:
        if type(attempt) is not int or not 1 <= attempt <= 2:
            return None
        safe["attempt"] = attempt
    status_code = candidate.get("status_code")
    if status_code is not None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            return None
        safe["status_code"] = status_code
    return safe


def _run_provider_check(
    *,
    repository: Path,
    container: str,
    result_path: Path | None,
    output: TextIO,
    error: TextIO,
    runner: CommandRunner,
    probe_bytes: bytes,
) -> int:
    result_file = _open_result_file(repository, result_path)
    if result_file is None:
        print("provider_check_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    result = _command(
        runner,
        ("docker", "exec", "-i", container, "python", "-", "--provider-check"),
        repository=repository,
        input_text=probe_bytes,
    )
    if result is None:
        _close_result_file(result_file)
        print("provider_check_exec=failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    rendered = _stdout_text(result)
    if rendered is None:
        _close_result_file(result_file)
        print("provider_check_output=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    if not _write_result_file(result_file, rendered):
        print("provider_check_output=write_failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    payload = _provider_check_payload(rendered)
    if payload is None:
        print("provider_check_result=invalid", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    print(json.dumps(payload, sort_keys=True), file=output)
    print(f"probe_exit_code={result.returncode}", file=output)
    return result.returncode


def run_server_gate(
    argv: Sequence[str],
    *,
    repository: Path,
    output: TextIO,
    error: TextIO,
    command_runner: CommandRunner = _run_command,
    expectations: CandidateExpectations = DEFAULT_EXPECTATIONS,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="masm-v11-api-1")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stability",
        action="store_true",
        help="After verification, make the fixed five-round real-Provider run",
    )
    mode.add_argument(
        "--diagnostic",
        action="store_true",
        help="After verification, run one round and print only safe aggregate diagnostics",
    )
    mode.add_argument(
        "--provider-check",
        action="store_true",
        help="After verification, make one call and print safe Provider failure metadata",
    )
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="Return the verification or probe status instead of shell-safe zero",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--criterion",
        choices=("frozen", "amended"),
        default="frozen",
        help=(
            "frozen (default) requires all five rounds to pass; amended applies the "
            "pre-registered criterion of handoff section 22"
        ),
    )
    args = parser.parse_args(list(argv))
    requested_mode = (
        "stability"
        if args.stability
        else "diagnostic"
        if args.diagnostic
        else "provider_check"
        if args.provider_check
        else None
    )
    if not _CONTAINER_NAME.fullmatch(args.container):
        print("container_name=invalid", file=error)
        print("verification_exit_code=2", file=output)
        if requested_mode is not None:
            print(f"{requested_mode}_not_started=yes", file=output)
        return 2 if args.strict_exit else 0

    verification_code, verified_probe = _verify_candidate(
        repository=repository,
        container=args.container,
        output=output,
        error=error,
        runner=command_runner,
        expectations=expectations,
    )
    print(f"verification_exit_code={verification_code}", file=output)
    if verification_code != 0:
        if requested_mode is not None:
            print(f"{requested_mode}_not_started=yes", file=output)
        return verification_code if args.strict_exit else 0
    if requested_mode is None:
        return 0
    if verified_probe is None:
        print(f"{requested_mode}_not_started=yes", file=output)
        return 2 if args.strict_exit else 0
    if args.provider_check:
        provider_check_code = _run_provider_check(
            repository=repository,
            container=args.container,
            result_path=args.output,
            output=output,
            error=error,
            runner=command_runner,
            probe_bytes=verified_probe,
        )
        return provider_check_code if args.strict_exit else 0
    if args.diagnostic:
        diagnostic_code = _run_diagnostic(
            repository=repository,
            container=args.container,
            result_path=args.output,
            output=output,
            error=error,
            runner=command_runner,
            probe_bytes=verified_probe,
        )
        return diagnostic_code if args.strict_exit else 0
    stability_code = _run_stability(
        repository=repository,
        container=args.container,
        result_path=args.output,
        output=output,
        error=error,
        runner=command_runner,
        probe_bytes=verified_probe,
        criterion=args.criterion,
    )
    return stability_code if args.strict_exit else 0


def main(argv: Sequence[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[1]
    return run_server_gate(
        sys.argv[1:] if argv is None else argv,
        repository=repository,
        output=sys.stdout,
        error=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
