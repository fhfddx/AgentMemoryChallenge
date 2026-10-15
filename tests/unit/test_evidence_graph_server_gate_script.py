"""Server gate stays read-only by default and never renders injected secrets."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import scripts.evidence_graph_server_gate as gate

_SOURCE_DIGEST = "c2a366822e455f7ea52310e54e26e34651b4c0495422a4419c268ebf33b82141"
_PROVIDERS = {
    "MASM_LLM_BASE_URL": "set",
    "MASM_LLM_API_KEY": "set",
    "MASM_EMBEDDING_BASE_URL": "set",
    "MASM_EMBEDDING_API_KEY": "set",
}


class FakeCommandRunner:
    """Replace only the external Git/Docker boundary; filesystem behavior remains real."""

    def __init__(
        self,
        *,
        source_digest: str = _SOURCE_DIGEST,
        providers: dict[str, str] | None = None,
        stability_code: int = 0,
    ) -> None:
        self.source_digest = source_digest
        self.providers = providers or dict(_PROVIDERS)
        self.stability_code = stability_code
        self.stability_inputs: list[bytes] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | bytes | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"):
            return subprocess.CompletedProcess(command, 0, "probe-blob\n", "")
        if command == ("git", "rev-parse", "HEAD"):
            return subprocess.CompletedProcess(command, 0, "candidate-head\n", "")
        if command == ("git", "ls-files", "-z", "--", "src/masm"):
            return subprocess.CompletedProcess(command, 0, "src/masm/a.py\0", "")
        if command == ("git", "status", "--porcelain", "--untracked-files=normal"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == (
            "docker",
            "inspect",
            "--format",
            "{{.Image}}",
            "masm-v11-api-1",
        ):
            return subprocess.CompletedProcess(command, 0, "sha256:candidate-image\n", "")
        if command == (
            "docker",
            "exec",
            "-i",
            "masm-v11-api-1",
            "python",
            "-",
        ):
            payload = {
                "candidate_modules": "present",
                "graph_flag": "unset(default_off)",
                "package_manifest": "match",
                "providers": self.providers,
                "runtime_profile": "official-masm",
                "source_digest": self.source_digest,
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload) + "\n", "")
        if command == (
            "docker",
            "exec",
            "-i",
            "masm-v11-api-1",
            "python",
            "-",
            "--stability",
        ):
            assert isinstance(input_text, bytes)
            self.stability_inputs.append(input_text)
            stdout = (
                '{"case":"summary","candidate_eligible":true}\n'
                '{"case":"stability_summary","round_count":5,'
                '"rounds_passed":5,"candidate_eligible":true}\n'
            )
            return subprocess.CompletedProcess(
                command,
                self.stability_code,
                stdout,
                "private-provider-body",
            )
        raise AssertionError(f"unexpected external command: {command!r}")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    (repository / "src" / "masm").mkdir(parents=True)
    (repository / "src" / "masm" / "a.py").write_bytes(b"value = 1\n")
    (repository / "scripts").mkdir()
    (repository / "scripts" / "evidence_graph_probe.py").write_bytes(
        b"print('synthetic probe')\n"
    )
    return repository


def _expectations() -> gate.CandidateExpectations:
    return gate.CandidateExpectations(
        probe_blob="probe-blob",
        probe_sha256="b4a8a6e328265ebaca707e56b05ecee7ecc89c324816f8122baf6790a9b21dee",
        source_digest=_SOURCE_DIGEST,
    )


def test_default_mode_verifies_without_starting_provider_calls(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        [],
        repository=_repository(tmp_path),
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 0
    assert runner.stability_inputs == []
    assert error.getvalue() == ""
    assert output.getvalue().splitlines() == [
        "repository_revision=present",
        "container_image=present",
        "checkout_clean=yes",
        "probe_blob_match=yes",
        "probe_worktree_match=yes",
        "source_manifest_match=yes",
        "source_match=yes",
        "MASM_RUNTIME_PROFILE=official-masm",
        "MASM_LLM_BASE_URL=set",
        "MASM_LLM_API_KEY=set",
        "MASM_EMBEDDING_BASE_URL=set",
        "MASM_EMBEDDING_API_KEY=set",
        "candidate_modules=present",
        "graph_flag=unset(default_off)",
        "verification_exit_code=0",
    ]
    assert "candidate-head" not in output.getvalue()
    assert "sha256:candidate-image" not in output.getvalue()


def test_source_mismatch_blocks_explicit_stability_run_without_leaking_output(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(source_digest="wrong-container-source")
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=_repository(tmp_path),
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 3
    assert runner.stability_inputs == []
    assert "source_match=no" in output.getvalue()
    assert "stability_not_started=yes" in output.getvalue()
    assert "wrong-container-source" not in output.getvalue()
    assert "wrong-container-source" not in error.getvalue()


def test_modified_worktree_probe_is_never_streamed_even_when_git_status_is_clean(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "scripts" / "evidence_graph_probe.py").write_bytes(b"tampered probe\n")
    runner = FakeCommandRunner()
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=repository,
        output=output,
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 3
    assert runner.stability_inputs == []
    assert "probe_worktree_match=no" in output.getvalue()
    assert "stability_not_started=yes" in output.getvalue()


def test_ignored_env_inside_package_is_rejected_without_reading_its_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    ignored_env = repository / "src" / "masm" / ".env"
    ignored_env.write_text("PRIVATE_VALUE=must-not-be-read\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == ignored_env:
            raise AssertionError("the gate attempted to read an ignored .env")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--strict-exit"],
        repository=repository,
        output=output,
        error=io.StringIO(),
        command_runner=FakeCommandRunner(),
        expectations=_expectations(),
    )

    assert code == 3
    assert "source_manifest_match=no" in output.getvalue()
    assert "must-not-be-read" not in output.getvalue()


def test_missing_provider_configuration_blocks_stability_without_values(
    tmp_path: Path,
) -> None:
    providers = dict(_PROVIDERS)
    providers["MASM_LLM_API_KEY"] = "missing"
    runner = FakeCommandRunner(providers=providers)
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=_repository(tmp_path),
        output=output,
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 2
    assert runner.stability_inputs == []
    assert "MASM_LLM_API_KEY=missing" in output.getvalue()
    assert "stability_not_started=yes" in output.getvalue()


def test_explicit_stability_run_uses_verified_probe_and_writes_sanitized_output(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    result_path = tmp_path / "stability.jsonl"
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--output", str(result_path)],
        repository=repository,
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 0
    assert runner.stability_inputs == [b"print('synthetic probe')\n"]
    assert result_path.read_text(encoding="utf-8").splitlines()[-1] == (
        '{"case":"stability_summary","round_count":5,'
        '"rounds_passed":5,"candidate_eligible":true}'
    )
    assert "stability_summary" in output.getvalue()
    assert "probe_exit_code=0" in output.getvalue()
    assert "private-provider-body" not in output.getvalue()
    assert "private-provider-body" not in error.getvalue()


def test_output_inside_checkout_is_rejected_before_provider_calls(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        [
            "--stability",
            "--strict-exit",
            "--output",
            str(repository / "stability.jsonl"),
        ],
        repository=repository,
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 2
    assert runner.stability_inputs == []
    assert "stability_output=invalid" in error.getvalue()
    assert "probe_exit_code=2" in output.getvalue()


def test_existing_output_is_not_followed_or_overwritten(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    result_path = tmp_path / "existing.jsonl"
    result_path.write_text("do-not-overwrite\n", encoding="utf-8")
    runner = FakeCommandRunner()

    code = gate.run_server_gate(
        [
            "--stability",
            "--strict-exit",
            "--output",
            str(result_path),
        ],
        repository=repository,
        output=io.StringIO(),
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 2
    assert runner.stability_inputs == []
    assert result_path.read_text(encoding="utf-8") == "do-not-overwrite\n"


def test_nonzero_probe_status_is_returned_without_rendering_provider_stderr(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(stability_code=1)
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        [
            "--stability",
            "--strict-exit",
            "--output",
            str(tmp_path / "stability.jsonl"),
        ],
        repository=_repository(tmp_path),
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 1
    assert "probe_exit_code=1" in output.getvalue()
    assert "private-provider-body" not in output.getvalue()
    assert "private-provider-body" not in error.getvalue()


def test_interactive_mode_reports_failed_probe_but_returns_shell_safe_zero(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(stability_code=1)
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--output", str(tmp_path / "stability.jsonl")],
        repository=_repository(tmp_path),
        output=output,
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 0
    assert "probe_exit_code=1" in output.getvalue()


def test_real_command_runner_delivers_stdin_and_discards_child_stderr(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    probe_bytes = b"verified\nprobe\n"
    result = gate._run_command(
        (
            sys.executable,
            "-c",
            (
                "import hashlib, sys; "
                "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()); "
                "sys.stderr.write('private-provider-body')"
            ),
        ),
        cwd=tmp_path,
        input_text=probe_bytes,
    )

    captured = capfd.readouterr()
    assert result.returncode == 0
    rendered = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    assert rendered.strip() == hashlib.sha256(probe_bytes).hexdigest()
    assert result.stderr is None
    assert "private-provider-body" not in captured.out
    assert "private-provider-body" not in captured.err


def test_real_git_boundary_returns_only_tracked_package_manifest(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    package = repository / "src" / "masm"
    package.mkdir(parents=True)
    (package / "tracked.py").write_bytes(b"value = 1\n")
    (package / ".env").write_text("PRIVATE_VALUE=ignored\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "server-gate-test@example.invalid"),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Server Gate Test"),
        cwd=repository,
        check=True,
    )
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=repository, check=True)

    result = gate._run_command(
        ("git", "ls-files", "-z", "--", "src/masm"),
        cwd=repository,
    )

    assert gate._tracked_package_files(result) == ("tracked.py",)
    assert "PRIVATE_VALUE" not in result.stdout


def test_actual_cli_is_shell_safe_unless_strict_exit_is_requested(tmp_path: Path) -> None:
    script = Path(gate.__file__).resolve()
    environment = dict(os.environ)

    interactive = subprocess.run(
        (sys.executable, str(script), "--container", "../invalid"),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    strict = subprocess.run(
        (sys.executable, str(script), "--container", "../invalid", "--strict-exit"),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert interactive.returncode == 0
    assert strict.returncode == 2
    assert "verification_exit_code=2" in interactive.stdout
    assert "verification_exit_code=2" in strict.stdout


def test_default_temp_directory_inside_checkout_is_rejected_before_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    checkout_temp = repository / "tmp"
    checkout_temp.mkdir()
    monkeypatch.setattr(gate.tempfile, "tempdir", str(checkout_temp))
    runner = FakeCommandRunner()
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=repository,
        output=output,
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 2
    assert runner.stability_inputs == []
    assert "probe_exit_code=2" in output.getvalue()


def test_flush_and_close_failures_are_converted_to_shell_safe_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingResult:
        def write(self, _: str) -> int:
            return 0

        def flush(self) -> None:
            raise OSError("private-filesystem-detail")

        def close(self) -> None:
            raise OSError("private-filesystem-detail")

    repository = _repository(tmp_path)
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()
    monkeypatch.setattr(gate, "_open_result_file", lambda *_: FailingResult())

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=repository,
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 2
    assert len(runner.stability_inputs) == 1
    assert "stability_output=write_failed" in error.getvalue()
    assert "private-filesystem-detail" not in error.getvalue()
    assert "probe_exit_code=2" in output.getvalue()
