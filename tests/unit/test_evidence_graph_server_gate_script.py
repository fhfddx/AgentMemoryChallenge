"""Server gate stays read-only by default and never renders injected secrets."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

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
        self.stability_inputs: list[str] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command == ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"):
            return subprocess.CompletedProcess(command, 0, "probe-blob\n", "")
        if command == ("git", "rev-parse", "HEAD"):
            return subprocess.CompletedProcess(command, 0, "candidate-head\n", "")
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
            assert input_text is not None
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
    (repository / "scripts" / "evidence_graph_probe.py").write_text(
        "print('synthetic probe')\n",
        encoding="utf-8",
    )
    return repository


def _expectations() -> gate.CandidateExpectations:
    return gate.CandidateExpectations(
        probe_blob="probe-blob",
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
        "host_head=candidate-head",
        "container_image=sha256:candidate-image",
        "checkout_clean=yes",
        "probe_blob_match=yes",
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
    assert runner.stability_inputs == ["print('synthetic probe')\n"]
    assert result_path.read_text(encoding="utf-8").splitlines()[-1] == (
        '{"case":"stability_summary","round_count":5,'
        '"rounds_passed":5,"candidate_eligible":true}'
    )
    assert "stability_summary" in output.getvalue()
    assert "probe_exit_code=0" in output.getvalue()
    assert "private-provider-body" not in output.getvalue()
    assert "private-provider-body" not in error.getvalue()


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
