"""Print the graph diagnostics for one search by capturing the log that carries them.

Search diagnostics are only written to the log, so a probe that talks to the HTTP API cannot see
them. This script runs one search in-process, captures the whitelisted payload that
``emit_search_diagnostics`` writes on the ``masm.search`` logger, and prints the graph fields. It
changes no API contract and returns counts and fixed enums only, never memory text.

It seeds the bridged two-session shape used by the bridge probe, runs the query without naming the
bridge, prints the graph verdict plus how many sessions the response covers, and cleans up.
"""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from masm.config import RuntimeProfile, Settings
from masm.runtime import build_runtime
from masm.schemas.api import SearchRequest
from masm.services.deletion_service import DeletionService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository
from masm.storage.types import EmbeddingDraft, MemoryBundle, MemoryDraft

GRAPH_FIELDS = (
    "graph_enabled",
    "graph_state",
    "graph_candidate_count",
    "graph_selected_count",
    "graph_bridge_added",
    "graph_link_added",
    "graph_bridge_skip",
    "graph_vetoed",
    "graph_fallback",
    "graph_failure_category",
    "graph_planner_failure_reason",
)


class _Capture(logging.Handler):
    """收集 emit_search_diagnostics 写出的白名单负载。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.rows: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, "extra", None)
        if isinstance(payload, dict):
            self.rows.append(payload)


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print graph diagnostics for one search")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--case",
        choices=("lexical", "semantic"),
        default="lexical",
        help="lexical: shared rare unit; semantic: paraphrase with no shared unit",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    settings = Settings.from_env()
    settings.validate_runtime()
    if settings.runtime_profile is not RuntimeProfile.OFFICIAL_MASM:
        print(json.dumps({"case": "preflight", "passed": False}), flush=True)
        return 2
    user_id = f"graph-diagnostics-probe-{uuid4().hex[:12]}"
    tag = uuid4().hex[:8]
    if args.case == "lexical":
        sister = f"Sister-{tag}"
        office = f"Office-{tag}"
        date = f"Date-{tag}"
        texts = (
            f"My sister {sister} works at the {office} office as an architect.",
            f"The {office} holiday party is scheduled for {date}.",
        )
        query = "When is my sister's company holiday party?"
    else:
        # 语义桥接：两段没有任何共同区分单元，只有 planner 能建立连接。
        texts = (
            "Priya signed off on the Barcelona trip for the whole design team.",
            "The Spanish offsite in November is confirmed for everyone.",
        )
        query = "When did Priya approve the Barcelona trip?"
    database = Database.create(settings.database_url)
    repository = MemoryRepository(database)
    deletion = DeletionService(repository, AssetStore(settings.asset_dir))
    run_ids: list[str] = []
    try:
        runtime = build_runtime(settings, repository)
        vectors = runtime.embeddings.embed_texts(list(texts))
        for text, vector in zip(texts, vectors, strict=True):
            request_id = f"{user_id}-{len(run_ids)}"
            run_ids.append(request_id)
            repository.add_bundle(
                user_id,
                MemoryBundle(
                    session_id=f"{user_id}-session-{len(run_ids) - 1}",
                    request_id=request_id,
                    messages=(),
                    memories=[
                        MemoryDraft(
                            summary=text,
                            original_text=text,
                            embedding=EmbeddingDraft(
                                modality="text",
                                model_name=runtime.embeddings.model_name,
                                model_version=getattr(
                                    runtime.embeddings, "model_version", "v1"
                                ),
                                dimensions=len(vector),
                                vector=vector,
                            ),
                        )
                    ],
                ),
            )
        service = SearchService(
            runtime.retriever,
            max_image_bytes=settings.max_image_bytes,
            analyzer=runtime.query_analyzer,
            expander=runtime.relation_expander,
            reranker=runtime.reranker,
            relevance_gate=runtime.relevance_gate,
            selector=runtime.evidence_selector,
            graph_selector=runtime.evidence_graph,
            runtime_profile=settings.runtime_profile.value,
        )
        logging.getLogger("masm.search").setLevel(logging.INFO)
        capture = _Capture()
        logging.getLogger("masm.search").addHandler(capture)
        try:
            response = service.search(
                SearchRequest(
                    query=query,
                    user_id=user_id,
                    top_k=args.top_k,
                )
            )
        finally:
            logging.getLogger("masm.search").removeHandler(capture)
        payload = capture.rows[-1] if capture.rows else {}
        sessions = {run_id for run_id in run_ids}
        returned = {item.id for item in response.data}
        print(
            json.dumps(
                {
                    "case": f"graph_diagnostics_{args.case}",
                    "returned_count": len(response.data),
                    "seeded_session_count": len(sessions),
                    "graph": {field: payload.get(field) for field in GRAPH_FIELDS},
                    "returned_id_count": len(returned),
                    "passed": bool(payload),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if payload else 1
    finally:
        for request_id in run_ids:
            try:
                deletion.delete_run(request_id, user_id=user_id)
            except Exception:
                pass
        database.engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for stdin-streamed execution inside the API container."""

    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
