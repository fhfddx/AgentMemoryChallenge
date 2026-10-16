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
        runtime_settings: str = "valid",
        runtime_setting_issues: list[str] | None = None,
        stability_code: int = 0,
        diagnostic_code: int = 1,
        provider_check_code: int = 1,
    ) -> None:
        self.source_digest = source_digest
        self.providers = providers or dict(_PROVIDERS)
        self.runtime_settings = runtime_settings
        self.runtime_setting_issues = runtime_setting_issues or []
        self.stability_code = stability_code
        self.diagnostic_code = diagnostic_code
        self.provider_check_code = provider_check_code
        self.stability_inputs: list[bytes] = []
        self.diagnostic_inputs: list[bytes] = []
        self.provider_check_inputs: list[bytes] = []

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
        if command == ("git", "cat-file", "blob", "HEAD:src/masm/a.py"):
            return subprocess.CompletedProcess(command, 0, b"value = 1\n", None)
        if command == (
            "git",
            "hash-object",
            "--path=src/masm/a.py",
            "--stdin",
        ):
            blob = "package-blob" if input_text == b"value = 1\n" else "different-blob"
            return subprocess.CompletedProcess(command, 0, f"{blob}\n".encode(), None)
        if command == ("git", "rev-parse", "HEAD:src/masm/a.py"):
            return subprocess.CompletedProcess(command, 0, "package-blob\n", "")
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
            if isinstance(input_text, bytes):
                self.diagnostic_inputs.append(input_text)
                stdout = (
                    '{"case":"private-case-name","positive":true,"passed":false,'
                    '"evidence_state":"sufficient","fallback":true,'
                    '"failure_category":"planner_invalid"}\n'
                    '{"case":"another-private-case","positive":false,"passed":true,'
                    '"evidence_state":"insufficient","fallback":false,'
                    '"failure_category":"none"}\n'
                    '{"case":"summary","case_count":2,"positive_total":1,'
                    '"positive_passed":0,"negative_total":1,"negative_passed":1,'
                    '"fallbacks":1,"failure_count":1,'
                    '"failure_reasons":{"graph_failure":1,"passed":1},'
                    '"state_transitions":{"insufficient->insufficient":1,'
                    '"sufficient->unknown":1},"candidate_eligible":false}\n'
                )
                return subprocess.CompletedProcess(
                    command,
                    self.diagnostic_code,
                    stdout,
                    "private-provider-body",
                )
            payload = {
                "candidate_modules": "present",
                "graph_flag": "unset(default_off)",
                "package_manifest": "match",
                "providers": self.providers,
                "runtime_profile": "official-masm",
                "runtime_settings": self.runtime_settings,
                "runtime_setting_issues": self.runtime_setting_issues,
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
        if command == (
            "docker",
            "exec",
            "-i",
            "masm-v11-api-1",
            "python",
            "-",
            "--provider-check",
        ):
            assert isinstance(input_text, bytes)
            self.provider_check_inputs.append(input_text)
            stdout = (
                '{"case":"provider_check","category":"http_status",'
                '"attempt":1,"status_code":401}\n'
            )
            return subprocess.CompletedProcess(
                command,
                self.provider_check_code,
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


def test_default_gate_pins_the_committed_probe() -> None:
    repository = Path(__file__).resolve().parents[2]
    probe = repository / "scripts" / "evidence_graph_probe.py"
    committed_blob = subprocess.run(
        ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert gate.DEFAULT_EXPECTATIONS.probe_blob == committed_blob
    assert gate._probe_bytes_match(
        probe.read_bytes(), gate.DEFAULT_EXPECTATIONS.probe_sha256
    )


def test_default_gate_pins_the_platform_independent_package_digest() -> None:
    repository = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ("git", "ls-files", "-z", "--", "src/masm"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    manifest = gate._tracked_package_files(tracked)
    assert manifest is not None
    manifest_matches, _ = gate._package_digest(repository / "src" / "masm", manifest)
    assert manifest_matches is True
    # 门禁与容器都比对「已提交字节」的摘要；worktree 的行尾（CRLF/LF）不能改变它。
    blob_digest = gate._git_package_digest(repository, manifest, gate._run_command)
    assert blob_digest is not None
    assert gate.DEFAULT_EXPECTATIONS.source_digest == blob_digest
    assert gate._worktree_matches_head(
        repository, repository / "src" / "masm", manifest, gate._run_command
    )


def test_probe_worktree_check_is_line_ending_independent() -> None:
    repository = Path(__file__).resolve().parents[2]
    probe = (repository / "scripts" / "evidence_graph_probe.py").read_bytes()
    pin = gate.DEFAULT_EXPECTATIONS.probe_sha256
    crlf = probe.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    assert gate._probe_bytes_match(probe, pin) is True
    assert gate._probe_bytes_match(crlf, pin) is True
    assert gate._probe_bytes_match(probe + b"\n# tampered\n", pin) is False
    assert gate._probe_bytes_match(probe.replace(b"import", b"imp0rt"), pin) is False


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
        "runtime_settings=valid",
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


def test_invalid_runtime_settings_block_stability_without_details(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner(
        runtime_settings="invalid",
        runtime_setting_issues=[
            "MASM_MAX_TOP_K:malformed_integer",
            "private-provider-body",
        ],
    )
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

    assert code == 2
    assert runner.stability_inputs == []
    assert "runtime_settings=invalid" in output.getvalue()
    assert "runtime_setting_issue=MASM_MAX_TOP_K:malformed_integer" in output.getvalue()
    assert "stability_not_started=yes" in output.getvalue()
    assert "private-malformed-value" not in output.getvalue()
    assert "private-provider-body" not in output.getvalue()
    assert "private-malformed-value" not in error.getvalue()
    assert "private-provider-body" not in error.getvalue()


def test_valid_runtime_ignores_stale_issue_payload(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        runtime_setting_issues=["MASM_MAX_TOP_K:malformed_integer"]
    )
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--strict-exit"],
        repository=_repository(tmp_path),
        output=output,
        error=io.StringIO(),
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 0
    assert "runtime_settings=valid" in output.getvalue()
    assert "runtime_setting_issue=" not in output.getvalue()


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


def test_explicit_diagnostic_runs_one_round_and_prints_only_safe_aggregates(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    result_path = tmp_path / "diagnostic.jsonl"
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        ["--diagnostic", "--strict-exit", "--output", str(result_path)],
        repository=repository,
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 1
    assert runner.diagnostic_inputs == [b"print('synthetic probe')\n"]
    assert json.loads(output.getvalue().splitlines()[-2]) == {
        "candidate_eligible": False,
        "case": "diagnostic_summary",
        "case_count": 2,
        "evidence_states": {"insufficient": 1, "sufficient": 1},
        "failure_categories": {"none": 1, "planner_invalid": 1},
        "failure_count": 1,
        "failure_reasons": {"graph_failure": 1, "passed": 1},
        "fallbacks": 1,
        "negative_passed": 1,
        "negative_total": 1,
        "positive_passed": 0,
        "positive_total": 1,
        "state_transitions": {
            "insufficient->insufficient": 1,
            "sufficient->unknown": 1,
        },
    }
    assert output.getvalue().splitlines()[-1] == "probe_exit_code=1"
    assert "private-case-name" not in output.getvalue()
    assert "another-private-case" not in output.getvalue()
    assert "private-provider-body" not in output.getvalue()
    assert "private-provider-body" not in error.getvalue()


def test_diagnostic_rejects_unknown_aggregate_keys_without_rendering_them(
    tmp_path: Path,
) -> None:
    class UnsafeAggregateRunner(FakeCommandRunner):
        def __call__(
            self,
            args: Sequence[str],
            *,
            cwd: Path,
            input_text: str | bytes | None = None,
        ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
            result = super().__call__(args, cwd=cwd, input_text=input_text)
            if tuple(args) == (
                "docker",
                "exec",
                "-i",
                "masm-v11-api-1",
                "python",
                "-",
            ) and isinstance(input_text, bytes):
                assert isinstance(result.stdout, str)
                return subprocess.CompletedProcess(
                    result.args,
                    result.returncode,
                    result.stdout.replace("graph_failure", "private-injected-key"),
                    result.stderr,
                )
            return result

    repository = _repository(tmp_path)
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        ["--diagnostic", "--strict-exit", "--output", str(tmp_path / "result.jsonl")],
        repository=repository,
        output=output,
        error=error,
        command_runner=UnsafeAggregateRunner(),
        expectations=_expectations(),
    )

    assert code == 2
    assert "diagnostic_summary=missing" in error.getvalue()
    assert "probe_exit_code=2" in output.getvalue()
    assert "private-injected-key" not in output.getvalue()
    assert "private-injected-key" not in error.getvalue()


def test_explicit_provider_check_runs_once_and_prints_only_safe_metadata(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    result_path = tmp_path / "provider-check.jsonl"
    runner = FakeCommandRunner()
    output = io.StringIO()
    error = io.StringIO()

    code = gate.run_server_gate(
        ["--provider-check", "--strict-exit", "--output", str(result_path)],
        repository=repository,
        output=output,
        error=error,
        command_runner=runner,
        expectations=_expectations(),
    )

    assert code == 1
    assert runner.provider_check_inputs == [b"print('synthetic probe')\n"]
    assert json.loads(output.getvalue().splitlines()[-2]) == {
        "attempt": 1,
        "case": "provider_check",
        "category": "http_status",
        "status_code": 401,
    }
    assert output.getvalue().splitlines()[-1] == "probe_exit_code=1"
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


def test_container_check_imports_actual_candidate_module_contract() -> None:
    repository = Path(gate.__file__).resolve().parents[1]
    tracked = gate._run_command(
        ("git", "ls-files", "-z", "--", "src/masm"),
        cwd=repository,
    )
    manifest = gate._tracked_package_files(tracked)
    assert manifest is not None

    result = gate._run_command(
        (sys.executable, "-"),
        cwd=repository,
        input_text=gate._container_check(manifest),
    )
    payload = gate._container_payload(result)

    assert payload is not None
    assert payload["candidate_modules"] == "present"
    assert payload["runtime_settings"] == "valid"


def test_container_check_sanitizes_malformed_numeric_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(gate.__file__).resolve().parents[1]
    tracked = gate._run_command(
        ("git", "ls-files", "-z", "--", "src/masm"),
        cwd=repository,
    )
    manifest = gate._tracked_package_files(tracked)
    assert manifest is not None
    monkeypatch.setenv("MASM_MAX_TOP_K", "private-malformed-integer")
    monkeypatch.setenv("MASM_MIN_TEXT_SIMILARITY", "private-malformed-float")

    result = gate._run_command(
        (sys.executable, "-"),
        cwd=repository,
        input_text=gate._container_check(manifest),
    )
    payload = gate._container_payload(result)

    assert payload is not None
    assert payload["runtime_settings"] == "invalid"
    assert payload["runtime_setting_issues"] == [
        "MASM_MAX_TOP_K:malformed_integer",
        "MASM_MIN_TEXT_SIMILARITY:malformed_float",
    ]
    assert "private-malformed-integer" not in str(result.stdout)
    assert "private-malformed-float" not in str(result.stdout)


def test_container_check_maps_runtime_validation_to_fixed_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(gate.__file__).resolve().parents[1]
    tracked = gate._run_command(
        ("git", "ls-files", "-z", "--", "src/masm"),
        cwd=repository,
    )
    manifest = gate._tracked_package_files(tracked)
    assert manifest is not None
    monkeypatch.setenv("MASM_MODEL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MASM_SELECTOR_TIMEOUT_SECONDS", "31")

    result = gate._run_command(
        (sys.executable, "-"),
        cwd=repository,
        input_text=gate._container_check(manifest),
    )
    payload = gate._container_payload(result)

    assert payload is not None
    assert payload["runtime_settings"] == "invalid"
    assert payload["runtime_setting_issues"] == [
        "configuration:selector_timeout_out_of_range"
    ]


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


def test_clean_crlf_checkout_uses_committed_blob_digest_for_source_match(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".gitattributes").write_bytes(b"*.py text eol=crlf\n")
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
    (repository / "src" / "masm" / "a.py").write_bytes(b"value = 1\r\n")
    subprocess.run(
        ("git", "add", "src/masm/a.py"),
        cwd=repository,
        check=True,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        cwd=repository,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert status.stdout == ""

    docker_runner = FakeCommandRunner()

    def git_backed_runner(
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | bytes | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if args[0] == "git":
            return gate._run_command(args, cwd=cwd, input_text=input_text)
        return docker_runner(args, cwd=cwd, input_text=input_text)

    probe_bytes = (repository / "scripts" / "evidence_graph_probe.py").read_bytes()
    probe_blob = subprocess.run(
        ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"),
        cwd=repository,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    ).stdout.strip()
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--strict-exit"],
        repository=repository,
        output=output,
        error=io.StringIO(),
        command_runner=git_backed_runner,
        expectations=gate.CandidateExpectations(
            probe_blob=probe_blob,
            probe_sha256=hashlib.sha256(probe_bytes).hexdigest(),
            source_digest=_SOURCE_DIGEST,
        ),
    )

    assert code == 0
    assert "source_manifest_match=yes" in output.getvalue()
    assert "source_match=yes" in output.getvalue()


def test_hidden_worktree_source_tampering_blocks_stability(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
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
    subprocess.run(
        ("git", "update-index", "--assume-unchanged", "src/masm/a.py"),
        cwd=repository,
        check=True,
    )
    (repository / "src" / "masm" / "a.py").write_bytes(b"tampered source\n")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        cwd=repository,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert status.stdout == ""

    docker_runner = FakeCommandRunner()

    def git_backed_runner(
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | bytes | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if args[0] == "git":
            return gate._run_command(args, cwd=cwd, input_text=input_text)
        return docker_runner(args, cwd=cwd, input_text=input_text)

    probe_bytes = (repository / "scripts" / "evidence_graph_probe.py").read_bytes()
    probe_blob = subprocess.run(
        ("git", "rev-parse", "HEAD:scripts/evidence_graph_probe.py"),
        cwd=repository,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    ).stdout.strip()
    output = io.StringIO()

    code = gate.run_server_gate(
        ["--stability", "--strict-exit"],
        repository=repository,
        output=output,
        error=io.StringIO(),
        command_runner=git_backed_runner,
        expectations=gate.CandidateExpectations(
            probe_blob=probe_blob,
            probe_sha256=hashlib.sha256(probe_bytes).hexdigest(),
            source_digest=_SOURCE_DIGEST,
        ),
    )

    assert code == 3
    assert docker_runner.stability_inputs == []
    assert "source_match=no" in output.getvalue()
    assert "stability_not_started=yes" in output.getvalue()


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
