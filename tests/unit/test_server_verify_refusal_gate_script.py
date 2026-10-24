"""服务器验证脚本：判定逻辑与退出码必须能在本地被伪造的探针输出驱动。"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
import scripts.server_verify_refusal_gate as verify


def _jsonl(rows: Sequence[dict[str, object]]) -> bytes:
    return ("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n").encode()


_GATE_STDOUT = b"\n".join(
    line.encode()
    for line in (
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
        "graph_flag=enabled",
        "verification_exit_code=0",
    )
) + b"\n"


def _abst_rows(*, positive_relation_returned: int = 1) -> list[dict[str, object]]:
    return [
        {"case": "storage", "passed": True},
        {"case": "positive_direct", "passed": True, "refusal_expected": False,
         "returned_count": 2},
        {"case": "positive_relation", "passed": True, "refusal_expected": False,
         "returned_count": positive_relation_returned},
        {"case": "relation_absent", "passed": True, "refusal_expected": True,
         "returned_count": 0},
        {"case": "cleanup", "passed": True},
    ]


def _bridge_rows(sessions: int = 2) -> list[dict[str, object]]:
    return [
        {"case": "storage", "passed": True},
        {"case": "bridged_question", "passed": True, "distinct_session_count": sessions,
         "returned_count": 1},
        {"case": "named_control", "passed": True, "distinct_session_count": sessions,
         "returned_count": 1},
        {"case": "unrelated_question", "passed": True, "returned_count": 0},
        {"case": "cleanup", "passed": True},
    ]


def _path_rows() -> list[dict[str, object]]:
    return [
        {"case": "storage", "passed": True},
        {"case": "connected_chain", "passed": True, "returned_count": 3},
        {"case": "disconnected_chain", "passed": True, "returned_count": 0},
        {"case": "cleanup", "passed": True},
    ]


def _compare_rows(*, forbidden_total: int = 0, subset_failures: int = 0) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = [
        {"case": f"case-{index:02d}", "subset_ok": index >= subset_failures}
        for index in range(26)
    ]
    cases.append({"case": "compare_summary", "forbidden_total": forbidden_total})
    return cases


def _diagnostic_rows(gate_hits: int = 3, hit_with_evidence: int = 0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "event": "search.completed",
            "candidate_count": 2,
            "refusal_gate": False,
            "selector_abstained": True,
            "returned_count": 0,
        }
    ]
    for index in range(gate_hits):
        rows.append(
            {
                "event": "search.completed",
                "candidate_count": 2,
                "refusal_gate": True,
                "selector_abstained": False,
                "returned_count": 1 if index < hit_with_evidence else 0,
            }
        )
    return rows


class FakeProbeRunner:
    """按探针脚本名返回伪造输出，不启动 docker。"""

    def __init__(self, payloads: dict[str, tuple[int, bytes]]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, container, script, arguments, log, budget):
        name = str(script).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        self.calls.append(name)
        return self.payloads[name]


@pytest.fixture()
def fake_runner(monkeypatch: pytest.MonkeyPatch):
    state = {"probe": None}

    def run(argv, *, input_bytes=None):
        argv = [str(part) for part in argv]
        if any(part.endswith("evidence_graph_server_gate.py") for part in argv):
            return verify.CommandResult(0, _GATE_STDOUT.decode(), "")
        if argv[:2] == ["docker", "exec"]:
            # 真实的脱敏格式化输出：写在 stderr，且带「时间 级别 logger」前缀。
            line = (
                '2026-10-24 23:00:00,000 INFO masm.search '
                '{"event": "search.completed", "refusal_gate": true, "returned_count": 0}'
            )
            return verify.CommandResult(0, "", line)
        if argv[:2] == ["docker", "logs"]:
            return verify.CommandResult(0, _jsonl(_diagnostic_rows()).decode(), "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(verify, "_run", run)
    monkeypatch.setattr(
        verify,
        "_run_probe",
        lambda container, script, arguments, log, budget: state["probe"](str(script)),
    )
    return state


def _set_probe(state, payloads: dict[str, tuple[int, bytes]]) -> None:
    state["probe"] = lambda script: payloads[script.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]]


def test_all_stages_pass_end_to_end(fake_runner, capsys) -> None:
    _set_probe(
        fake_runner,
        {
            "abstention_path_probe.py": (0, _jsonl(_abst_rows())),
            "multi_session_bridge_probe.py": (0, _jsonl(_bridge_rows())),
            "multi_session_path_probe.py": (0, _jsonl(_path_rows())),
            "evidence_graph_probe.py": (0, _jsonl(_compare_rows())),
        },
    )

    exit_code = verify.main(["all"])

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "RESULT=PASS" in stdout
    assert "[abst   ] PASS" in stdout
    assert "[bridge1] PASS" in stdout
    assert "[observe] INFO" in stdout


def test_bridge_needs_two_sessions_and_fails_otherwise(fake_runner, capsys) -> None:
    _set_probe(
        fake_runner,
        {"multi_session_bridge_probe.py": (0, _jsonl(_bridge_rows(sessions=1)))},
    )

    exit_code = verify.main(["bridge"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert stdout.count("[bridge1] FAIL") + stdout.count("[bridge2] FAIL") == 2
    assert "RESULT=FAIL" in stdout


def test_positive_relation_without_evidence_fails_the_abstention_stage(
    fake_runner, capsys
) -> None:
    _set_probe(
        fake_runner,
        {
            "abstention_path_probe.py": (
                0,
                _jsonl(_abst_rows(positive_relation_returned=0)),
            )
        },
    )

    exit_code = verify.main(["abst"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "[abst   ] FAIL" in stdout
    assert "positive_relation_returned=0" in stdout


def test_compare_forbidden_total_above_five_fails(fake_runner, capsys) -> None:
    _set_probe(
        fake_runner,
        {"evidence_graph_probe.py": (0, _jsonl(_compare_rows(forbidden_total=6)))},
    )

    exit_code = verify.main(["compare"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "forbidden_total=6" in stdout


def test_compare_subset_failure_is_named(fake_runner, capsys) -> None:
    _set_probe(
        fake_runner,
        {"evidence_graph_probe.py": (0, _jsonl(_compare_rows(subset_failures=1)))},
    )

    exit_code = verify.main(["compare"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "case-00" in stdout


def test_gate_mismatch_is_reported_with_the_offending_field(
    fake_runner, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv, *, input_bytes=None):
        return verify.CommandResult(0, _GATE_STDOUT.decode().replace(
            "source_match=yes", "source_match=no"
        ), "")

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["gate"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "source_match=no" in stdout


def test_gate_requires_every_provider_variable(fake_runner, capsys, monkeypatch) -> None:
    def run(argv, *, input_bytes=None):
        broken = _GATE_STDOUT.decode().replace(
            "MASM_LLM_BASE_URL=set", "MASM_LLM_BASE_URL=missing"
        )
        return verify.CommandResult(0, broken, "")

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["gate"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "providers=3/4" in stdout


def test_observe_flags_a_gate_hit_that_returned_evidence(fake_runner, capsys, monkeypatch) -> None:
    def run(argv, *, input_bytes=None):
        return verify.CommandResult(
            0, _jsonl(_diagnostic_rows(gate_hits=2, hit_with_evidence=1)).decode(), ""
        )

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["observe"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "gate_hits_with_evidence=1" in stdout


def test_observe_fails_when_diagnostics_lack_the_new_field(
    fake_runner, capsys, monkeypatch
) -> None:
    rows = [{"event": "search.completed", "candidate_count": 3, "returned_count": 1}]

    def run(argv, *, input_bytes=None):
        return verify.CommandResult(0, _jsonl(rows).decode(), "")

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["observe"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "refusal_gate_field_missing" in stdout


def test_field_check_reads_the_redacted_stderr_line(fake_runner, capsys, monkeypatch) -> None:
    line = (
        '2026-10-24 23:00:00,000 INFO masm.search '
        '{"event": "search.completed", "refusal_gate": true}'
    )

    def run(argv, *, input_bytes=None):
        return verify.CommandResult(0, "", line)

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["field"])

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "[field  ] PASS" in stdout


def test_field_check_fails_when_the_whitelist_drops_the_field(
    fake_runner, capsys, monkeypatch
) -> None:
    line = '2026-10-24 23:00:00,000 INFO masm.search {"event": "search.completed"}'

    def run(argv, *, input_bytes=None):
        return verify.CommandResult(0, "", line)

    monkeypatch.setattr(verify, "_run", run)

    exit_code = verify.main(["field"])

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "refusal_gate_field=missing" in stdout


def test_unknown_stage_is_rejected_by_the_parser(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        verify.main(["bogus"])

    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
