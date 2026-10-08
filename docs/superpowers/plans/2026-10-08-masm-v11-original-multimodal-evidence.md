# MASM v1.1 Original Multimodal Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return selected message memories as their original ordered text/image evidence with verified inline image bytes and safe summary fallback.

**Architecture:** The repository batches immutable source-message and asset metadata snapshots, `AssetStore` performs confined integrity-checked reads, and `EvidenceRenderer` substitutes original content after ranking but before byte-aware packing. Rendering is deterministic and failure-safe; the public API stays unchanged.

**Tech Stack:** Python 3.11+, dataclasses, Pydantic content models, SQLAlchemy/PostgreSQL, local content-addressed storage, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-08-masm-v11-original-multimodal-evidence-design.md`

## Global Constraints

- Do not alter the official Add/Search request or response schemas.
- Keep source-message and asset SQL scoped by `user_id`.
- Limit batched locations to 256 and rendered image bytes to the configured per-image limit.
- Verify object confinement, recorded size, and SHA-256 before rendering.
- Treat each source message atomically; on any rendering failure retain the ranked summary.
- Render after ranking and before actual-JSON byte packing.
- Never expose object keys, filesystem paths, or error details in the response.

## Review Focus

- A symlink/junction or `..` object key must never escape the asset root; Task 1 pins real-path and lexical checks.
- A source message referencing an asset row from another user must fall back rather than cross the user boundary; Task 2 pins user isolation.
- Duplicate asset rows for a content-addressed key must not make output nondeterministic; Task 1 pins stable metadata selection.
- One invalid part in a mixed source message must prevent partial source disclosure; Task 2 pins atomic fallback.
- Base64 expansion can exceed the response limit even when raw bytes do not; Task 3 pins actual serialized-size packing.

---

### Task 1: User-scoped source snapshots and verified object reads

**Files:**
- Modify: `src/masm/storage/types.py`
- Modify: `src/masm/storage/repositories.py`
- Modify: `src/masm/storage/assets.py`
- Test: `tests/unit/storage/test_asset_store.py`
- Test: `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Produces: `StoredAssetSnapshot`, `SourceMessageSnapshot`, `MemoryRepository.source_messages_for_positions(user_id: str, positions: Sequence[tuple[str, int]]) -> Mapping[tuple[str, int], SourceMessageSnapshot]`, and `AssetStore.read_object(relative: str, *, max_bytes: int, expected_size: int, expected_hash: str) -> bytes`.
- Consumes: existing immutable `SourceMessage` JSON and `Asset` metadata.

- [ ] **Step 1: Write failing storage tests for batched user isolation, de-duplication, safe read, traversal, size mismatch, hash mismatch, and missing objects**

- [ ] **Step 2: Run focused storage tests and verify RED**

Run: `python -m pytest tests/unit/storage/test_asset_store.py tests/integration/test_retrieval_enhancements.py -q`

Expected: FAIL because the snapshot and read APIs do not exist.

- [ ] **Step 3: Implement snapshot types, batched repository lookup, and integrity-checked object reads**

Use SQL tuple membership for distinct locations, a second user-scoped asset query, stable first metadata per sorted asset row, and existing root-confinement helpers.

- [ ] **Step 4: Run focused storage tests and verify GREEN**

Run: `python -m pytest tests/unit/storage/test_asset_store.py tests/integration/test_retrieval_enhancements.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/masm/storage/types.py src/masm/storage/repositories.py src/masm/storage/assets.py tests/unit/storage/test_asset_store.py tests/integration/test_retrieval_enhancements.py
git commit -m "feat: load verified original evidence sources"
```

### Task 2: Atomic original-evidence renderer

**Files:**
- Create: `src/masm/retrieval/evidence_renderer.py`
- Modify: `src/masm/retrieval/reranker.py`
- Modify: `src/masm/services/search_service.py`
- Create: `tests/unit/retrieval/test_evidence_renderer.py`
- Modify: `tests/unit/retrieval/test_conflict_reranking.py`

**Interfaces:**
- Consumes: Task 1 snapshot/read interfaces and `RankedEvidence` provenance.
- Produces: `EvidenceRenderer.render(user_id: str, evidence: Sequence[RankedEvidence]) -> list[RankedEvidence]`; `RankedEvidence.content: str | list[ContentPart]` plus `granularity`, `request_id`, and `source_position`.

- [ ] **Step 1: Write failing tests for exact text, ordered text-image-text, unchanged non-message evidence, atomic fallback, and preserved ranking metadata**

- [ ] **Step 2: Run renderer tests and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_renderer.py tests/unit/retrieval/test_conflict_reranking.py -q`

Expected: FAIL because renderer/provenance fields do not exist.

- [ ] **Step 3: Implement provenance propagation and atomic rendering**

Use typed `TextPart`/`ImageURLPart`, Base64 encoding, `dataclasses.replace`, and catch only validation/storage/read failures that require summary fallback.

- [ ] **Step 4: Run renderer tests and verify GREEN**

Run: `python -m pytest tests/unit/retrieval/test_evidence_renderer.py tests/unit/retrieval/test_conflict_reranking.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/masm/retrieval/evidence_renderer.py src/masm/retrieval/reranker.py src/masm/services/search_service.py tests/unit/retrieval/test_evidence_renderer.py tests/unit/retrieval/test_conflict_reranking.py
git commit -m "feat: render original multimodal evidence"
```

### Task 3: Application wiring, byte-aware integration, and regression gates

**Files:**
- Modify: `src/masm/api/app.py`
- Modify: `tests/integration/test_retrieval_enhancements.py`
- Modify: `tests/unit/retrieval/test_response_packer.py`
- Modify: `tests/unit/test_runtime_factory.py`

**Interfaces:**
- Consumes: Task 2 `EvidenceRenderer` and the existing `ResponsePacker`.
- Produces: application-factory Search wiring using the same repository, asset store, and `Settings.max_image_bytes`.

- [ ] **Step 1: Write failing Add-to-Search integration and rendered-size packing tests**

Assert the returned ordered parts contain an inline Data URL whose decoded bytes equal the original PNG, wrong-user source lookup retains the summary, and an oversized rendered image is skipped while a later short item remains.

- [ ] **Step 2: Run focused integration tests and verify RED**

Run: `python -m pytest tests/integration/test_retrieval_enhancements.py tests/unit/retrieval/test_response_packer.py tests/unit/test_runtime_factory.py -q`

Expected: FAIL because the application does not inject the renderer or rendered list content is not yet exercised.

- [ ] **Step 3: Wire `EvidenceRenderer` in `create_app` and complete packing compatibility**

- [ ] **Step 4: Run focused integration tests and verify GREEN**

Run: `python -m pytest tests/integration/test_retrieval_enhancements.py tests/unit/retrieval/test_response_packer.py tests/unit/test_runtime_factory.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Run full quality gates**

Run: `python -m pytest -q`

Expected: at least 659 passed, with only the existing platform-specific skip.

Run: `ruff check .`

Expected: no errors.

Run: `mypy src scripts/cloud_smoke_recovery.py scripts/delete_evaluation_run.py`

Expected: no errors.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/masm/api/app.py tests/integration/test_retrieval_enhancements.py tests/unit/retrieval/test_response_packer.py tests/unit/test_runtime_factory.py
git commit -m "feat: wire original evidence into search responses"
```

