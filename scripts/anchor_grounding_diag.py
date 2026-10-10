"""Diagnostic: legacy set-level anchor validation vs the deployed grounding rule.

Reproduces the two synthetic shapes that matter, then evaluates the *same* selector with
the deployed rule and with the 015986a set-level rule, recording the raw model output so the
comparison can be attributed. Content-safe: prints counts, indices and enums only, never the
synthetic text. Cleans every registered run.
"""

import json
from collections.abc import Sequence
from uuid import uuid4

import httpx

from masm.config import Settings
from masm.retrieval.evidence_selector import (
    EvidenceSelector,
    _anchor_tokens,
    _evidence_text,
)
from masm.retrieval.selector_pool import build_selector_pool
from masm.runtime import build_runtime
from masm.schemas.api import SearchRequest
from masm.services.deletion_service import DeletionService
from masm.services.search_service import SearchService
from masm.storage.assets import AssetStore
from masm.storage.db import Database
from masm.storage.repositories import MemoryRepository


def row(**kwargs: object) -> None:
    print(json.dumps(kwargs, sort_keys=True), flush=True)


def legacy_grounded(
    self: EvidenceSelector, question: str, selected: Sequence[object]
) -> tuple[object, ...]:
    """015986a 行为：只校验整组选中文本的并集包含问题锚点。"""
    anchors = _anchor_tokens(question)
    chosen = tuple(selected)
    if not anchors:
        return chosen
    union: frozenset[str] = frozenset()
    for item in chosen:
        union |= _anchor_tokens(_evidence_text(item))  # type: ignore[arg-type]
    return chosen if anchors <= union else ()


def evaluate(
    service: SearchService,
    spy: list[dict[str, object]],
    *,
    label: str,
    user_id: str,
    query: str,
    expected: tuple[str, ...],
    forbidden: tuple[str, ...],
    selector_max_candidates: int,
    variant: str,
) -> None:
    request = SearchRequest(query=query, user_id=user_id, top_k=10)
    parsed = service._analyze(request)  # noqa: SLF001
    recalled, _ = service._retriever.retrieve_with_stats(user_id, parsed, 30)  # noqa: SLF001
    candidates = [item for item in recalled if item.user_id == user_id]
    if service._relevance_gate is not None:  # noqa: SLF001
        candidates = service._relevance_gate.filter(candidates)  # noqa: SLF001
    strong = {item.memory_id for item in candidates}
    if service._expander is not None and candidates:  # noqa: SLF001
        candidates = service._with_expansion(user_id, candidates, 10)  # noqa: SLF001
    if service._expander is not None or service._reranker is not None:  # noqa: SLF001
        candidates = service._deduplicate(candidates)  # noqa: SLF001
    ranked = service._rank(parsed, candidates)  # noqa: SLF001
    pool = build_selector_pool(ranked, max_candidates=selector_max_candidates)
    pool_text = " ".join(_evidence_text(item) for item in pool)

    selector = service._selector
    assert isinstance(selector, EvidenceSelector)
    original = selector._grounded_selection  # noqa: SLF001
    installed = variant == "legacy"
    if installed:
        selector._anchor_connectivity = None  # noqa: SLF001
        selector._grounded_selection = legacy_grounded.__get__(selector, EvidenceSelector)  # type: ignore[method-assign]  # noqa: SLF001, E501
    del spy[:]
    try:
        selection = selector.select(query, None, ranked, strong)
    finally:
        if installed:
            selector._grounded_selection = original  # type: ignore[method-assign]  # noqa: SLF001
    text = " ".join(_evidence_text(item) for item in selection.evidence)
    row(
        case="diag",
        scenario=label,
        variant=variant,
        pool_count=len(pool),
        pool_expected=sum(marker in pool_text for marker in expected),
        pool_forbidden=sum(marker in pool_text for marker in forbidden),
        raw=list(spy),
        returned_count=len(selection.evidence),
        returned_expected=sum(marker in text for marker in expected),
        returned_forbidden=sum(marker in text for marker in forbidden),
        state=selection.evidence_state,
        fallback=selection.fallback,
        abstained=selection.abstained,
    )


def add_runs(
    client: httpx.Client, user_id: str, texts: Sequence[str], registered: list[str]
) -> bool:
    ok = True
    for index, text in enumerate(texts):
        run_id = f"{user_id}-{index}"
        registered.append(run_id)
        response = client.post(
            "/add",
            json={
                "request_id": run_id,
                "user_id": user_id,
                "session_id": f"{user_id}-s{index}",
                "messages": [{"role": "user", "content": text}],
            },
        )
        ok &= response.status_code == 200 and response.json().get("success") is True
    return ok


def main() -> int:
    settings = Settings.from_env()
    settings.validate_runtime()
    database = Database.create(settings.database_url)
    repository = MemoryRepository(database)
    deletion = DeletionService(repository, AssetStore(settings.asset_dir))
    runtime = build_runtime(settings, repository)
    selector = runtime.evidence_selector
    assert isinstance(selector, EvidenceSelector)

    scenario_tag = uuid4().hex[:12]
    search_user = f"diag-anchor-search-{scenario_tag}"
    multihop_user = f"diag-anchor-multihop-{scenario_tag}"
    project = f"Atlas-{scenario_tag}"
    code_name = f"Cobalt-{scenario_tag}"
    cabinet = f"Cabinet-Seven-{scenario_tag}"
    review_date = "November 14, 2026"
    search_texts = (
        f"The {project} migration uses code name {code_name}, and its dossier "
        f"is stored in {cabinet}.",
        f"The review for that migration is scheduled on {review_date}.",
    )
    chain_tag = uuid4().hex[:12]
    root, bridge = f"Root-{chain_tag}", f"Bridge-{chain_tag}"
    leaf, terminal = f"Leaf-{chain_tag}", f"Terminal-{chain_tag}"
    missing = f"Unconnected-{chain_tag}"
    multihop_texts = (
        f"The registered chain {root} continues through {bridge}.",
        f"For the same registered chain, {bridge} continues through {leaf}.",
        f"The terminal record associated with {leaf} is {terminal}.",
        f"The isolated catalog entry {missing} uses registered-chain terminology "
        "but belongs to a separate archive.",
    )

    registered: list[str] = []
    spy: list[dict[str, object]] = []
    original_complete = selector._llm.complete_json  # noqa: SLF001

    def spy_complete(request: object, schema: object) -> object:
        output = original_complete(request, schema)  # type: ignore[arg-type]
        spy.append(
            {
                "state": output.evidence_state,  # type: ignore[attr-defined]
                "count": len(output.selected_indices),  # type: ignore[attr-defined]
                "indices": list(output.selected_indices),  # type: ignore[attr-defined]
            }
        )
        return output

    selector._llm.complete_json = spy_complete  # type: ignore[method-assign]  # noqa: SLF001
    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        headers={"X-Api-Key": settings.api_keys[0]},
        timeout=240,
    )
    try:
        row(
            case="setup",
            search_added=add_runs(client, search_user, search_texts, registered),
            multihop_added=add_runs(client, multihop_user, multihop_texts, registered),
        )
        service = SearchService(
            runtime.retriever,
            max_image_bytes=settings.max_image_bytes,
            analyzer=runtime.query_analyzer,
            expander=runtime.relation_expander,
            reranker=runtime.reranker,
            relevance_gate=runtime.relevance_gate,
            selector=selector,
            runtime_profile=settings.runtime_profile.value,
        )
        scenarios = (
            (
                "search_path_multi_session",
                search_user,
                f"What is the {project} migration code name, and when is its review?",
                (code_name, review_date),
                (),
            ),
            (
                "multihop_disconnected",
                multihop_user,
                f"What terminal record is linked to {missing}?",
                (missing,),
                (root, bridge, leaf, terminal),
            ),
        )
        for label, user_id, query, expected, forbidden in scenarios:
            for variant in ("new", "legacy"):
                evaluate(
                    service,
                    spy,
                    label=label,
                    user_id=user_id,
                    query=query,
                    expected=expected,
                    forbidden=forbidden,
                    selector_max_candidates=settings.selector_max_candidates,
                    variant=variant,
                )
    finally:
        selector._llm.complete_json = original_complete  # noqa: SLF001
        residual_memories = 0
        residual_sources = 0
        errors = 0
        for _ in range(3):
            residual_memories = 0
            residual_sources = 0
            for run_id in registered:
                user_id = search_user if run_id.startswith(search_user) else multihop_user
                try:
                    report = deletion.delete_run(run_id, user_id=user_id)
                    residual_memories += report.memories_deleted
                    residual_sources += report.sources_deleted
                except Exception:
                    errors += 1
        row(
            case="cleanup",
            registered=len(registered),
            residual_memories=residual_memories,
            residual_sources=residual_sources,
            errors=errors,
            passed=residual_memories == 0 and residual_sources == 0 and errors == 0,
        )
        client.close()
        runtime.close()
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
