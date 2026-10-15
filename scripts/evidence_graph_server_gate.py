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
    "source_digest": source_digest,
}, sort_keys=True))
"""


@dataclass(frozen=True)
class CandidateExpectations:
    probe_blob: str
    probe_sha256: str
    source_digest: str


DEFAULT_EXPECTATIONS = CandidateExpectations(
    probe_blob="ac29d661ab93d918ad0a5ff1412548121d44cbee",
    probe_sha256="7b9358e3027a87d8e31bc8a50a35164fb6bf06d0f57780952e9811fbaf4ccf35",
    source_digest="6a0c96dd09c0eef2dfbb412828a5d46bca4cd1c5d6e7b840f272ce50775dae94",
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


def _container_check(manifest: Sequence[str]) -> str:
    return _CONTAINER_CHECK_TEMPLATE.replace(
        "PACKAGE_MANIFEST_JSON",
        json.dumps(list(manifest)),
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
    probe_worktree_matches = hashlib.sha256(probe_bytes).hexdigest() == expectations.probe_sha256
    container_manifest_matches = payload.get("package_manifest") == "match"
    source_manifest_matches = host_manifest_matches and container_manifest_matches
    container_digest = payload.get("source_digest")
    source_matches = (
        source_manifest_matches
        and host_digest == expectations.source_digest
        and container_digest == expectations.source_digest
    )
    runtime_profile = payload.get("runtime_profile")
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
        or candidate_modules != "present"
        or any(provider_statuses.get(name) != "set" for name in _PROVIDER_VARIABLES)
    ):
        return 2, None
    return 0, probe_bytes


def _run_stability(
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
    parser.add_argument(
        "--stability",
        action="store_true",
        help="After verification, make the fixed five-round real-Provider run",
    )
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="Return the verification or probe status instead of shell-safe zero",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv))
    if not _CONTAINER_NAME.fullmatch(args.container):
        print("container_name=invalid", file=error)
        print("verification_exit_code=2", file=output)
        if args.stability:
            print("stability_not_started=yes", file=output)
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
        if args.stability:
            print("stability_not_started=yes", file=output)
        return verification_code if args.strict_exit else 0
    if not args.stability:
        return 0
    if verified_probe is None:
        print("stability_not_started=yes", file=output)
        return 2 if args.strict_exit else 0
    stability_code = _run_stability(
        repository=repository,
        container=args.container,
        result_path=args.output,
        output=output,
        error=error,
        runner=command_runner,
        probe_bytes=verified_probe,
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
