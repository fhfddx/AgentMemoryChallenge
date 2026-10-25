"""服务器侧验证：拒答预检候选（不消耗官方 Smoke 额度）。

用法（在 /root/AgentMemoryChallenge 下，宿主机执行）：

    python3 scripts/server_verify_refusal_gate.py
    python3 scripts/server_verify_refusal_gate.py all            # 加上 abstention 探针
    python3 scripts/server_verify_refusal_gate.py abst compare   # 只跑指定阶段
    python3 scripts/server_verify_refusal_gate.py --since 6h     # 观测窗口（默认 180m）

每个阶段打印一行摘要，最后一行是 ``RESULT=PASS`` 或 ``RESULT=FAIL``；退出码 0/1。
长探针用独立会话启动并从宿主流入容器，SSH 断线不会中断它；重连后用同样的命令重跑即可。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

# 已验证的测量伪影：在字节完全相同的选择器上，这一例会随 provider 抖动在
# 25/26 与 26/26 之间翻转（v7 有时保留含 forbidden 片段的引用，加法式把它剔除，
# 于是"超集"不成立但结果更合规）。只有"恰好一例、且它就是这例、且 merged 未含
# forbidden 片段"时降级为 WARN；任何正例掉证据仍然是 FAIL。
KNOWN_FLAKY_NEGATIVES = frozenset({"elliptical_confirmation_negative"})

# 脱敏格式化后是 "2026-10-24 23:00:00,000 INFO masm.search {...}"，冒号后可能有空格。
_FIELD_PATTERN = re.compile(r'"refusal_gate":\s*true')

DEFAULT_STAGES = ("gate", "field", "bridge", "path", "compare", "observe")
ALL_STAGES = (
    "gate",
    "field",
    "abst",
    "bridge",
    "path",
    "compare",
    "observe",
    "profile",
    "detail",
)
PROBE_TIMEOUTS = {"abst": 420.0, "bridge": 420.0, "path": 420.0, "compare": 540.0}
PROBE_SCRIPTS = {
    "abst": "abstention_path_probe.py",
    "bridge": "multi_session_bridge_probe.py",
    "path": "multi_session_path_probe.py",
    "compare": "evidence_graph_probe.py",
}
PROBE_ARGUMENTS = {
    "abst": ("--timeout", "240"),
    "bridge": ("--timeout", "240"),
    "path": ("--timeout", "240"),
    "compare": ("--compare",),
}
_PROVIDER_LINES = (
    "MASM_LLM_BASE_URL",
    "MASM_LLM_API_KEY",
    "MASM_EMBEDDING_BASE_URL",
    "MASM_EMBEDDING_API_KEY",
)
_GATE_FIELDS = (
    "checkout_clean",
    "probe_blob_match",
    "probe_worktree_match",
    "source_manifest_match",
    "source_match",
    "MASM_RUNTIME_PROFILE",
    "runtime_settings",
    "candidate_modules",
    "graph_flag",
    "verification_exit_code",
)
_FIELD_CHECK = """
import logging

from masm.observability.logging import configure_logging
from masm.retrieval.diagnostics import SearchDiagnostics, emit_search_diagnostics

# 走生产同一条脱敏链路：白名单少一个键，这里就会看不到 refusal_gate。
configure_logging(level=logging.INFO)
emit_search_diagnostics(
    SearchDiagnostics(
        request_tag="0" * 32,
        runtime_profile="official-masm",
        candidate_count=0,
        dedup_count=0,
        returned_count=0,
        response_bytes=0,
        latency_ms=0.0,
        status_code=200,
        channel_counts={},
        refusal_gate=True,
    )
)
"""


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Row:
    verdict: str
    stage: str
    detail: str


def _run(argv: Sequence[str], *, input_bytes: bytes | None = None) -> CommandResult:
    """执行一条外部命令；只在这一层碰 subprocess，测试可替换。"""

    completed = subprocess.run(  # noqa: S603 - 参数固定，无 shell
        list(argv),
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=900,
    )
    return CommandResult(
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def _run_probe(
    container: str,
    script: Path,
    arguments: Sequence[str],
    log: Path,
    budget: float,
) -> tuple[int | str, bytes]:
    """把宿主上的探针脚本流进容器执行；独立会话，超时则终止。"""

    with script.open("rb") as stdin_handle, log.open("wb") as output_handle:
        process = subprocess.Popen(  # noqa: S603 - 参数固定，无 shell
            ["docker", "exec", "-i", container, "python", "-", *arguments],
            stdin=stdin_handle,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + budget
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(5)
        if process.poll() is None:
            process.kill()
            process.wait()
            return "timeout", log.read_bytes()
    return process.returncode if process.returncode is not None else -1, log.read_bytes()


def parse_rows(payload: bytes | str) -> list[dict[str, object]]:
    """从每一行里取出 JSON 对象；兼容纯 JSONL 与 ``时间 级别 logger {json}`` 前缀。"""

    return [row for _, row in parse_timed_rows(payload)]


_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s+")


def parse_timed_rows(payload: bytes | str) -> list[tuple[str | None, dict[str, object]]]:
    """同 :func:`parse_rows`，但在 ``docker logs -t`` 前缀存在时一并取出时间戳。"""

    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
    decoder = json.JSONDecoder()
    rows: list[tuple[str | None, dict[str, object]]] = []
    for line in text.splitlines():
        stamp: str | None = None
        match = _TIMESTAMP.match(line)
        if match is not None:
            stamp = match.group(1)
            line = line[match.end():]
        start = line.find("{")
        if start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append((stamp, value))
    return rows


def _failed_cases(rows: Sequence[dict[str, object]]) -> list[str]:
    return [str(row.get("case")) for row in rows if row.get("passed") is False]


def summarize_gate(result: CommandResult) -> Row:
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in _GATE_FIELDS:
            values[name] = value.strip()
    providers = sum(
        1 for name in _PROVIDER_LINES if f"{name}=set" in result.stdout.split()
    )
    expected = {
        "checkout_clean": "yes",
        "probe_blob_match": "yes",
        "probe_worktree_match": "yes",
        "source_manifest_match": "yes",
        "source_match": "yes",
        "MASM_RUNTIME_PROFILE": "official-masm",
        "runtime_settings": "valid",
        "candidate_modules": "present",
        "verification_exit_code": "0",
    }
    mismatched = [
        f"{name}={values.get(name, 'missing')}"
        for name, wanted in expected.items()
        if values.get(name) != wanted
    ]
    ok = result.returncode == 0 and not mismatched and providers == len(_PROVIDER_LINES)
    detail = (
        f"exit={result.returncode} providers={providers}/{len(_PROVIDER_LINES)} "
        f"graph_flag={values.get('graph_flag', 'missing')}"
    )
    if mismatched:
        detail += " mismatched=" + ",".join(mismatched[:5])
    if ok:
        return Row(PASS, "gate", detail)
    tail = result.stderr.strip().splitlines()[-1:] or [""]
    return Row(FAIL, "gate", (detail + " stderr=" + tail[0])[:240])


def summarize_field(result: CommandResult) -> Row:
    # logging 默认写 stderr，脱敏格式化后的行还带 ``时间 级别 logger`` 前缀。
    combined = result.stdout + result.stderr
    if _FIELD_PATTERN.search(combined):
        return Row(PASS, "field", "refusal_gate_field=present")
    tail = (combined.strip().splitlines() or [""])[-1]
    detail = f"refusal_gate_field=missing exit={result.returncode} tail={tail[:120]}"
    return Row(FAIL, "field", detail)


def summarize_abst(exit_code: int | str, rows: Sequence[dict[str, object]]) -> Row:
    failed = _failed_cases(rows)
    positives = {
        str(row.get("case")): row for row in rows if row.get("refusal_expected") is False
    }
    relation = positives.get("positive_relation")
    relation_returned = relation.get("returned_count") if relation is not None else "missing"
    ok = (
        exit_code == 0
        and bool(rows)
        and not failed
        and all(row.get("passed") is True for row in rows)
        # 正例必须真的有证据：预检若误伤关系人存在的问题，这一条会先红。
        and isinstance(relation_returned, int)
        and relation_returned >= 1
    )
    relation_returned = relation.get("returned_count") if relation is not None else "missing"
    detail = (
        f"exit={exit_code} rows={len(rows)} fail={len(failed)} "
        f"positive_relation_returned={relation_returned} "
        f"failed_cases={','.join(failed[:4]) or 'none'}"
    )
    return Row(PASS if ok else FAIL, "abst", detail)


def summarize_bridge(exit_code: int | str, rows: Sequence[dict[str, object]], stage: str) -> Row:
    by_case = {str(row.get("case")): row for row in rows}
    failed = _failed_cases(rows)
    bridged = by_case.get("bridged_question")
    sessions = bridged.get("distinct_session_count") if bridged is not None else None
    ok = (
        exit_code == 0
        and not failed
        and bridged is not None
        and bridged.get("passed") is True
        and sessions == 2
    )
    bridged_returned = bridged.get("returned_count") if bridged is not None else "missing"
    detail = (
        f"exit={exit_code} bridged_sessions={sessions} bridged_returned={bridged_returned} "
        f"fail={len(failed)} failed_cases={','.join(failed[:4]) or 'none'}"
    )
    return Row(PASS if ok else FAIL, stage, detail)


def summarize_path(exit_code: int | str, rows: Sequence[dict[str, object]]) -> Row:
    by_case = {str(row.get("case")): row for row in rows}
    failed = _failed_cases(rows)
    disconnected = by_case.get("disconnected_chain")
    connected = by_case.get("connected_chain")
    ok = (
        exit_code == 0
        and not failed
        and disconnected is not None
        and disconnected.get("passed") is True
        and connected is not None
        and connected.get("passed") is True
    )
    disconnected_returned = (
        disconnected.get("returned_count") if disconnected is not None else "missing"
    )
    connected_returned = connected.get("returned_count") if connected is not None else "missing"
    detail = (
        f"exit={exit_code} disconnected_returned={disconnected_returned} "
        f"connected_returned={connected_returned} "
        f"fail={len(failed)} failed_cases={','.join(failed[:4]) or 'none'}"
    )
    return Row(PASS if ok else FAIL, "path", detail)


def summarize_compare(exit_code: int | str, rows: Sequence[dict[str, object]]) -> Row:
    summaries = [row for row in rows if row.get("case") == "compare_summary"]
    cases = [row for row in rows if "subset_ok" in row]
    ok_count = sum(1 for row in cases if row.get("subset_ok") is True)
    bad = [str(row.get("case")) for row in cases if row.get("subset_ok") is False]
    forbidden = summaries[-1].get("forbidden_total") if summaries else None
    ok = (
        exit_code == 0
        and len(cases) >= 26
        and not bad
        and isinstance(forbidden, int)
        and forbidden <= 5
    )
    forbidden_text = forbidden if forbidden is not None else "missing"
    detail = (
        f"exit={exit_code} subset_ok={ok_count}/{len(cases)} false={len(bad)} "
        f"forbidden_total={forbidden_text} false_cases={','.join(bad[:4]) or 'none'}"
    )
    if bad:
        shapes = ";".join(
            f"{row.get('case')}(baseline={row.get('baseline_count')},"
            f"merged={row.get('merged_count')},added={row.get('added_count')},"
            f"forbidden={row.get('forbidden')},state={row.get('graph_state')})"
            for row in cases
            if row.get("subset_ok") is False
        )
        detail += f" shape={shapes[:200]}"
    if ok:
        return Row(PASS, "compare", detail)
    failing = [
        row for row in cases if row.get("subset_ok") is False
    ]
    if (
        len(failing) == 1
        and str(failing[0].get("case")) in KNOWN_FLAKY_NEGATIVES
        and failing[0].get("forbidden") is False
    ):
        return Row(WARN, "compare", detail + " known_flaky=measured_variance")
    return Row(FAIL, "compare", detail)


def _bucket(value: object, edges: Sequence[int]) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        return "unknown"
    for edge in edges:
        if value <= edge:
            return f"<={edge}"
    return f">{edges[-1]}"


def _histogram(rows: Sequence[dict[str, object]], field: str, edges: Sequence[int]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        key = _bucket(row.get(field), edges)
        counts[key] = counts.get(key, 0) + 1
    return str(dict(sorted(counts.items())))


def _is_diagnostic(row: dict[str, object]) -> bool:
    return row.get("event") == "search.completed" or "candidate_count" in row


def summarize_detail(timed: Sequence[tuple[str | None, dict[str, object]]]) -> list[Row]:
    """逐题列出无文本的计数指纹：区分「检索层没给候选」与「有候选但被丢弃」。"""

    rows: list[Row] = []
    index = 0
    for stamp, row in timed:
        if not _is_diagnostic(row):
            continue
        index += 1
        clock = stamp[11:19] if stamp else "?"
        channels = row.get("channel_counts") or {}
        rows.append(
            Row(
                INFO,
                "detail",
                f"#{index} {clock} cand={row.get('candidate_count')} "
                f"dedup={row.get('dedup_count')} returned={row.get('returned_count')} "
                f"gate={row.get('refusal_gate')} sel_abst={row.get('selector_abstained')} "
                f"sel_state={row.get('selector_evidence_state')} "
                f"gstate={row.get('graph_state')} gcand={row.get('graph_candidate_count')} "
                f"planner={row.get('graph_planner_calls')} "
                f"req={row.get('graph_requirement_count')} "
                f"sup={row.get('graph_support_count')} link={row.get('graph_link_count')} "
                f"bridge={row.get('graph_bridge_added')}/{row.get('graph_bridge_skip')} "
                f"retry={row.get('retry_added')} best_effort={row.get('best_effort')} "
                f"ch={channels}",
            )
        )
    if not rows:
        return [Row(INFO, "detail", "no_search_diagnostics_in_window")]
    return rows


def summarize_profile(rows: Sequence[dict[str, object]]) -> list[Row]:
    """把一次官方运行的诊断折叠成不含文本的指纹：多少题、几题空、候选与图状态分布。"""

    diagnostics = [row for row in rows if _is_diagnostic(row)]
    if not diagnostics:
        return [Row(INFO, "profile", "no_search_diagnostics_in_window")]
    empty = sum(1 for row in diagnostics if row.get("returned_count") == 0)
    gate_hits = sum(1 for row in diagnostics if row.get("refusal_gate") is True)
    abstained = sum(1 for row in diagnostics if row.get("selector_abstained") is True)
    graph_on = sum(1 for row in diagnostics if row.get("graph_enabled") is True)
    states: dict[str, int] = {}
    for row in diagnostics:
        state = str(row.get("graph_state", "unknown"))
        states[state] = states.get(state, 0) + 1
    return [
        Row(
            INFO,
            "profile",
            f"searches={len(diagnostics)} empty={empty} gate_hits={gate_hits} "
            f"selector_abstained={abstained} graph_enabled={graph_on}",
        ),
        Row(
            INFO,
            "profile",
            "candidate_count=" + _histogram(diagnostics, "candidate_count", (0, 4, 16)),
        ),
        Row(
            INFO,
            "profile",
            "returned_count=" + _histogram(diagnostics, "returned_count", (0, 1, 3)),
        ),
        Row(INFO, "profile", f"graph_state={dict(sorted(states.items()))}"),
    ]


def _cluster(timed: Sequence[tuple[str | None, dict[str, object]]], gap_seconds: float):
    """按时间间隔把日志切成簇：一次官方运行与一次探针不会落进同一簇。"""

    clusters: list[list[tuple[str | None, dict[str, object]]]] = []
    previous: float | None = None
    for stamp, row in timed:
        moment = _epoch(stamp)
        if previous is None or moment is None or moment - previous > gap_seconds:
            clusters.append([])
        clusters[-1].append((stamp, row))
        if moment is not None:
            previous = moment
    return clusters


def _epoch(stamp: str | None) -> float | None:
    if stamp is None:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_clusters(
    timed: Sequence[tuple[str | None, dict[str, object]]], gap_seconds: float = 150.0
) -> list[Row]:
    """逐簇指纹：把探针流量与官方运行分开，免得把探针的 gate_hits 算到官方头上。"""

    clusters = [
        [(stamp, row) for stamp, row in cluster]
        for cluster in _cluster(timed, gap_seconds)
        if any(_is_diagnostic(row) for _, row in cluster)
    ]
    if len(clusters) < 2:
        return []
    rows: list[Row] = []
    for index, cluster in enumerate(clusters, start=1):
        # 只数诊断行：普通日志（http.request / model.call）不是一次搜索。
        diagnostics = [row for _, row in cluster if _is_diagnostic(row)]
        stamp = cluster[0][0]
        clock = stamp[11:19] if stamp else "unknown"
        empty = sum(1 for row in diagnostics if row.get("returned_count") == 0)
        gate_hits = sum(1 for row in diagnostics if row.get("refusal_gate") is True)
        abstained = sum(1 for row in diagnostics if row.get("selector_abstained") is True)
        candidates = _histogram(diagnostics, "candidate_count", (0, 4, 16))
        rows.append(
            Row(
                INFO,
                "profile",
                f"cluster{index} start={clock} searches={len(diagnostics)} empty={empty} "
                f"gate_hits={gate_hits} selector_abstained={abstained} candidates={candidates}",
            )
        )
    return rows


def summarize_observe(rows: Sequence[dict[str, object]]) -> Row:
    diagnostics = [row for row in rows if _is_diagnostic(row)]
    with_field = [row for row in diagnostics if "refusal_gate" in row]
    hits = hits_with_evidence = abstained = 0
    for row in with_field:
        if row.get("refusal_gate") is True:
            hits += 1
            if row.get("returned_count"):
                hits_with_evidence += 1
        elif row.get("selector_abstained") is True:
            abstained += 1
    if not diagnostics:
        return Row(INFO, "observe", "no_search_diagnostics_in_window")
    if not with_field:
        return Row(
            FAIL,
            "observe",
            f"diagnostics={len(diagnostics)} refusal_gate_field_missing"
            " (白名单缺键或镜像未更新)",
        )
    detail = (
        f"diagnostics={len(diagnostics)} with_field={len(with_field)} "
        f"gate_hits={hits} gate_hits_with_evidence={hits_with_evidence} "
        f"selector_abstained={abstained}"
    )
    return Row(FAIL if hits_with_evidence else INFO, "observe", detail)


def _probe_stage(
    name: str,
    container: str,
    probe_dir: Path,
    work_dir: Path,
    probe_runner: Callable[..., tuple[int | str, bytes]],
) -> tuple[int | str, bytes]:
    script = probe_dir / PROBE_SCRIPTS[name]
    log = work_dir / f"{name}.log"
    return probe_runner(
        container,
        script,
        PROBE_ARGUMENTS[name],
        log,
        PROBE_TIMEOUTS[name],
    )


def run_stages(
    stages: Sequence[str],
    *,
    container: str,
    since: str,
    repo_dir: Path,
    work_dir: Path,
    runner: Callable[..., CommandResult] | None = None,
    probe_runner: Callable[..., tuple[int | str, bytes]] | None = None,
) -> list[Row]:
    runner = _run if runner is None else runner
    probe_runner = _run_probe if probe_runner is None else probe_runner
    probe_dir = repo_dir / "scripts"
    results: list[Row] = []
    for stage in stages:
        if stage == "gate":
            results.append(
                summarize_gate(
                    runner(
                        (
                            sys.executable,
                            str(probe_dir / "evidence_graph_server_gate.py"),
                            "--container",
                            container,
                            "--strict-exit",
                        )
                    )
                )
            )
        elif stage == "field":
            results.append(
                summarize_field(
                    runner(
                        ("docker", "exec", "-i", container, "python", "-"),
                        input_bytes=_FIELD_CHECK.encode(),
                    )
                )
            )
        elif stage == "observe":
            # docker logs 把容器的 stdout 与 stderr 分别写到 CLI 的同名流；
            # 应用日志走 stderr，所以两路都要读。
            result = runner(("docker", "logs", "--since", since, container))
            results.append(summarize_observe(parse_rows(result.stdout + result.stderr)))
        elif stage == "profile":
            result = runner(("docker", "logs", "-t", "--since", since, container))
            timed = parse_timed_rows(result.stdout + result.stderr)
            results.extend(summarize_profile([row for _, row in timed]))
            results.extend(summarize_clusters(timed))
        elif stage == "detail":
            result = runner(("docker", "logs", "-t", "--since", since, container))
            results.extend(summarize_detail(parse_timed_rows(result.stdout + result.stderr)))
        elif stage == "bridge":
            # 桥接判据有抖动：连跑两次，两次都必须命中 2 个会话。
            for run in (1, 2):
                exit_code, payload = _probe_stage(
                    "bridge", container, probe_dir, work_dir, probe_runner
                )
                results.append(
                    summarize_bridge(exit_code, parse_rows(payload), f"bridge{run}")
                )
        elif stage in PROBE_SCRIPTS:
            exit_code, payload = _probe_stage(
                stage, container, probe_dir, work_dir, probe_runner
            )
            rows = parse_rows(payload)
            if stage == "abst":
                results.append(summarize_abst(exit_code, rows))
            elif stage == "path":
                results.append(summarize_path(exit_code, rows))
            else:
                results.append(summarize_compare(exit_code, rows))
        else:  # pragma: no cover - 参数解析已挡住
            raise ValueError(f"unknown stage: {stage}")
    return results


def _print_rows(rows: Sequence[Row]) -> None:
    for row in rows:
        print(f"[{row.stage:<7}] {row.verdict:<4} {row.detail[:320]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the refusal-gate candidate on the server")
    parser.add_argument("--container", default="masm-v11-api-1")
    parser.add_argument("--since", default="180m", help="observation window for docker logs")
    parser.add_argument(
        "stages",
        nargs="*",
        choices=(*ALL_STAGES, "all"),
        help="stages to run; default: " + " ".join(DEFAULT_STAGES),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    stages: list[str] = []
    for stage in args.stages or DEFAULT_STAGES:
        for expanded in ALL_STAGES if stage == "all" else (stage,):
            if expanded not in stages:
                stages.append(expanded)
    repo_dir = Path(__file__).resolve().parents[1]
    print(
        f"container={args.container} stages={','.join(stages)} repo={repo_dir}",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="masm-verify.") as work:
        rows = run_stages(
            stages,
            container=args.container,
            since=args.since,
            repo_dir=repo_dir,
            work_dir=Path(work),
        )
    _print_rows(rows)
    failed = [row.stage for row in rows if row.verdict == FAIL]
    if failed:
        print(f"RESULT=FAIL failed={','.join(failed)}")
        return 1
    print("RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
