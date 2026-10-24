"""Large-store abstention probe: does a noisy memory store make refusals fail?

The existing abstention probe seeds four memories, so the relevance gate admits all of them and
the selector sees a tiny, clean pool. The official store is far larger (the metered run showed
110 text and 21 image memories for one user, with 55 to 82 candidates surviving the gate), and
there the refusal questions mostly return evidence.

This probe reproduces the official condition: seed one user with a noisy store of several dozen
memories, then ask the same refusal shapes plus two positives. A refusal case that returns any
evidence is a failure. Everything is registered before transmission and cleaned up in three
rounds, so a failed run still removes its own data.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TextIO
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from masm.config import RuntimeProfile, Settings
from masm.services.deletion_service import DeletionReport, DeletionService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _Client(Protocol):
    def post(self, path: str, *, json: dict[str, Any]) -> _Response: ...


DeleteRun = Callable[..., DeletionReport]

_TOPIC_WORDS = (
    "newsletter", "roof repair", "cycle route", "book club", "pottery class",
    "dentist", "train ticket", "garden", "harbour", "bakery",
)


def _post(client: _Client, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"{path} status")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} body")
    return body


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    return " ".join(parts)


def _print_row(output: TextIO, row: dict[str, Any]) -> None:
    print(json.dumps(row, sort_keys=True), file=output)


def _corpus(run_tag: str, topics: int) -> list[str]:
    """一段有噪声但主题自洽的记忆库：目标主体只记录代码与地点，从不记录颜色/数量。"""
    texts = [
        f"The access code for the Item-{run_tag} locker is Code-{run_tag}.",
        f"The Item-{run_tag} locker is on the third floor of the Harbour building.",
    ]
    for index in range(topics):
        word = _TOPIC_WORDS[index % len(_TOPIC_WORDS)]
        texts.append(
            f"Notes about the {word} {run_tag}-{index} are kept in the shared folder."
        )
        texts.append(
            f"The {word} {run_tag}-{index} meeting happens on the "
            f"{index + 1}th of the month."
        )
    return texts


def _cases(run_tag: str, absent: str) -> tuple[tuple[str, str], ...]:
    return (
        ("positive_code", f"What is the access code for the Item-{run_tag} locker?"),
        ("positive_location", f"Where is the Item-{run_tag} locker?"),
        # 主体全库不存在。
        ("absent_subject", f"What is the access code for the Item-{absent} locker?"),
        # 主体存在但颜色从未记录。
        ("absent_attribute", f"What colour is the Item-{run_tag} locker?"),
        # 同类属性存在（地点），只是数量从未记录。
        ("absent_quantity", f"How many shelves does the Item-{run_tag} locker have?"),
        # 主体不存在的关系人。
        ("relation_absent", f"What does my sister {absent.capitalize()} do for work?"),
        # 措辞贴近库内文本、但答案不在库里。
        ("option_overlap", f"Which city hosts the newsletter {run_tag}-0 office?"),
        # 时间从未记录。
        ("absent_temporal", f"When did the locker Item-{run_tag} get installed?"),
    )


def run_probe(
    client: _Client,
    repository: MemoryRepository,
    delete_run: DeleteRun,
    output: TextIO,
    *,
    run_tag: str,
    topics: int,
) -> int:
    user_id = f"abstention-noise-probe-{run_tag}"
    absent = f"Missing{uuid4().hex[:8].capitalize()}"
    texts = _corpus(run_tag, topics)
    run_ids = [f"{user_id}-{index}" for index in range(len(texts))]
    registered: list[str] = []
    all_passed = True
    cleanup_passed = False
    failure_exit_code = 0
    try:
        for index, (run_id, content) in enumerate(zip(run_ids, texts, strict=True)):
            registered.append(run_id)
            body = _post(
                client,
                "/add",
                {
                    "request_id": run_id,
                    "user_id": user_id,
                    "session_id": f"{user_id}-session-{index // 4}",
                    "messages": [{"role": "user", "content": content}],
                },
            )
            if body.get("success") is not True:
                raise RuntimeError("Add contract")

        contexts = repository.context_candidates_for_requests(user_id, run_ids)
        messages = repository.message_candidates_for_requests(user_id, run_ids, 256)
        _print_row(
            output,
            {
                "case": "storage",
                "registered": len(registered),
                "context_count": len(contexts),
                "message_count": len(messages),
                "passed": len(contexts) > 0,
            },
        )

        for name, query in _cases(run_tag, absent):
            body = _post(
                client,
                "/search",
                {"query": query, "user_id": user_id, "top_k": 10},
            )
            data = body.get("data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise RuntimeError("Search contract")
            returned = " ".join(_content_text(row.get("content")) for row in data)
            refusal = name.startswith("absent") or name in {
                "relation_absent",
                "option_overlap",
            }
            if refusal:
                passed = len(data) == 0
            elif name == "positive_code":
                passed = f"Code-{run_tag}" in returned
            else:
                passed = "Harbour building" in returned
            all_passed &= passed
            _print_row(
                output,
                {
                    "case": name,
                    "returned_count": len(data),
                    "refusal_expected": refusal,
                    "passed": passed,
                },
            )
    except RuntimeError:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {"case": "probe", "failure_category": "request_or_contract", "passed": False},
        )
    except Exception:
        all_passed = False
        failure_exit_code = 2
        _print_row(
            output,
            {"case": "probe", "failure_category": "internal_error", "passed": False},
        )
    finally:

        def cleanup_round() -> tuple[list[DeletionReport], int]:
            reports: list[DeletionReport] = []
            errors = 0
            for run_id in registered:
                try:
                    reports.append(delete_run(run_id, user_id=user_id))
                except Exception:
                    errors += 1
            return reports, errors

        first, first_errors = cleanup_round()
        retry, retry_errors = cleanup_round()
        residual, residual_errors = cleanup_round()
        residual_memories = sum(report.memories_deleted for report in residual)
        residual_sources = sum(report.sources_deleted for report in residual)
        cleanup_passed = (
            residual_errors == 0
            and len(residual) == len(registered)
            and all(report.complete for report in residual)
            and residual_memories == 0
            and residual_sources == 0
        )
        _print_row(
            output,
            {
                "case": "cleanup",
                "registered_run_count": len(registered),
                "cleanup_attempt_error_count": first_errors + retry_errors + residual_errors,
                "first_pass_memories_deleted": sum(r.memories_deleted for r in first),
                "residual_memories_deleted": residual_memories,
                "complete": cleanup_passed,
                "passed": cleanup_passed,
            },
        )
    if failure_exit_code:
        return failure_exit_code
    return 0 if all_passed and cleanup_passed else 1


def _local_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("probe base URL must be local")
    return value.rstrip("/")


def main(argv: Sequence[str] | None = None) -> int:
    """Run inside the deployed API container against its loopback endpoint."""

    parser = argparse.ArgumentParser(description="Run the large-store abstention probe")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--topics", type=int, default=10)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        settings = Settings.from_env()
        settings.validate_runtime()
        if (
            settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM
            or not settings.database_url
            or not settings.api_keys
            or args.timeout <= 0
            or args.topics < 1
        ):
            raise ValueError("incomplete probe configuration")
        base_url = _local_base_url(args.base_url)
    except Exception:
        _print_row(sys.stdout, {"case": "preflight", "passed": False})
        return 2

    database: Database | None = None
    try:
        database = Database.create(settings.database_url)
        repository = MemoryRepository(database)
        deletion = DeletionService(repository, AssetStore(settings.asset_dir))
        with httpx.Client(
            base_url=base_url,
            headers={"X-Api-Key": settings.api_keys[0]},
            timeout=args.timeout,
        ) as client:
            return run_probe(
                client,
                repository,
                deletion.delete_run,
                sys.stdout,
                run_tag=uuid4().hex[:10],
                topics=args.topics,
            )
    except Exception:
        _print_row(sys.stdout, {"case": "setup", "passed": False})
        return 2
    finally:
        if database is not None:
            database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
