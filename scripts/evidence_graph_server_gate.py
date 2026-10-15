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
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, cast

_PROVIDER_VARIABLES = (
    "MASM_LLM_BASE_URL",
    "MASM_LLM_API_KEY",
    "MASM_EMBEDDING_BASE_URL",
    "MASM_EMBEDDING_API_KEY",
)
_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_DEFAULT_RESULT_PATH = Path("/tmp/masm-evidence-graph-stability.jsonl")
_CONTAINER_CHECK = r"""
import hashlib
import json
import os
from pathlib import Path

import masm

root = Path(masm.__file__).resolve().parent
digest = hashlib.sha256()
files = (
    path for path in root.rglob("*")
    if path.is_file()
    and "__pycache__" not in path.parts
    and path.suffix != ".pyc"
)
for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")

try:
    from masm.retrieval.evidence_graph import EvidenceGraphPlanner
    from masm.retrieval.evidence_graph_assembly import assemble_evidence
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
    "providers": {
        name: "set" if os.environ.get(name) else "missing"
        for name in provider_names
    },
    "runtime_profile": (
        "official-masm"
        if os.environ.get("MASM_RUNTIME_PROFILE") == "official-masm"
        else "not_official_masm"
    ),
    "source_digest": digest.hexdigest(),
}, sort_keys=True))
"""


@dataclass(frozen=True)
class CandidateExpectations:
    probe_blob: str
    source_digest: str


DEFAULT_EXPECTATIONS = CandidateExpectations(
    probe_blob="ac29d661ab93d918ad0a5ff1412548121d44cbee",
    source_digest="6a0c96dd09c0eef2dfbb412828a5d46bca4cd1c5d6e7b840f272ce50775dae94",
)


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def _run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
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


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = (
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _command(
    runner: CommandRunner,
    args: Sequence[str],
    *,
    repository: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(args, cwd=repository, input_text=input_text)
    except OSError:
        return None


def _clean_line(result: subprocess.CompletedProcess[str] | None) -> str | None:
    if result is None or result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _container_payload(
    result: subprocess.CompletedProcess[str] | None,
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
) -> int:
    probe_path = repository / "scripts" / "evidence_graph_probe.py"
    package_root = repository / "src" / "masm"
    if not probe_path.is_file() or not package_root.is_dir():
        print("candidate_checkout=invalid", file=error)
        return 2

    probe_blob = _clean_line(
        _command(
            runner,
            ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"),
            repository=repository,
        )
    )
    host_head = _clean_line(
        _command(runner, ("git", "rev-parse", "HEAD"), repository=repository)
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
        input_text=_CONTAINER_CHECK,
    )
    payload = _container_payload(container_result)
    if host_head is None or status is None or status.returncode != 0 or image is None:
        print("verification_error=command_failed", file=error)
        return 2
    if payload is None:
        print("container_check=failed", file=error)
        return 2

    checkout_clean = not status.stdout.strip()
    probe_matches = probe_blob == expectations.probe_blob
    container_digest = payload.get("source_digest")
    source_matches = (
        _source_digest(package_root) == expectations.source_digest
        and container_digest == expectations.source_digest
    )
    runtime_profile = payload.get("runtime_profile")
    candidate_modules = payload.get("candidate_modules")
    graph_flag = payload.get("graph_flag")
    providers = payload.get("providers")
    provider_statuses = providers if isinstance(providers, dict) else {}

    print(f"host_head={host_head}", file=output)
    print(f"container_image={image}", file=output)
    print(f"checkout_clean={'yes' if checkout_clean else 'no'}", file=output)
    print(f"probe_blob_match={'yes' if probe_matches else 'no'}", file=output)
    print(f"source_match={'yes' if source_matches else 'no'}", file=output)
    print(f"MASM_RUNTIME_PROFILE={runtime_profile}", file=output)
    for name in _PROVIDER_VARIABLES:
        value = provider_statuses.get(name)
        print(f"{name}={value if value in {'set', 'missing'} else 'invalid'}", file=output)
    print(f"candidate_modules={candidate_modules}", file=output)
    print(f"graph_flag={graph_flag}", file=output)

    if not checkout_clean or not probe_matches or not source_matches:
        return 3
    if (
        runtime_profile != "official-masm"
        or candidate_modules != "present"
        or any(provider_statuses.get(name) != "set" for name in _PROVIDER_VARIABLES)
    ):
        return 2
    return 0


def _run_stability(
    *,
    repository: Path,
    container: str,
    result_path: Path,
    output: TextIO,
    error: TextIO,
    runner: CommandRunner,
) -> int:
    probe_text = (repository / "scripts" / "evidence_graph_probe.py").read_text(
        encoding="utf-8"
    )
    result = _command(
        runner,
        ("docker", "exec", "-i", container, "python", "-", "--stability"),
        repository=repository,
        input_text=probe_text,
    )
    if result is None:
        print("stability_exec=failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2
    try:
        result_path.write_text(result.stdout, encoding="utf-8")
    except OSError:
        print("stability_output=write_failed", file=error)
        print("probe_exit_code=2", file=output)
        return 2

    lines = [line for line in result.stdout.splitlines() if line.strip()]
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
    parser.add_argument("--output", type=Path, default=_DEFAULT_RESULT_PATH)
    args = parser.parse_args(list(argv))
    if not _CONTAINER_NAME.fullmatch(args.container):
        print("container_name=invalid", file=error)
        print("verification_exit_code=2", file=output)
        if args.stability:
            print("stability_not_started=yes", file=output)
        return 2 if args.strict_exit else 0

    verification_code = _verify_candidate(
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
    stability_code = _run_stability(
        repository=repository,
        container=args.container,
        result_path=args.output,
        output=output,
        error=error,
        runner=command_runner,
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
