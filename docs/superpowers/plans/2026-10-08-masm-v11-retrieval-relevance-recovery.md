# MASM v1.1 Retrieval Relevance Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Search preserve absolute relevance, execute decomposed/options-aware queries independently, abstain on weak history, and stop message granularity from overpowering relevance.

**Architecture:** `SearchService` creates bounded query variants, `BaselineRetriever` merges per-variant recall into one list per logical channel and attaches raw signals, and an injectable `RelevanceGate` admits strong anchors before relation expansion. Reranking retains its existing adjustments but uses granularity only as an exact-score tie-break.

**Tech Stack:** Python 3.11+, dataclasses, FastAPI/Pydantic, SQLAlchemy/PostgreSQL/pgvector, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-08-masm-v11-retrieval-relevance-recovery-design.md`

## Global Constraints

- Do not change the official Add/Search request or response schemas.
- Do not generate or rewrite answers in `/search`.
- Keep every database read scoped by `user_id` in SQL.
- Cap total text query variants at eight and keep provider batches at or below ten.
- Enable relevance gating only for `official-masm` by default; local-fake behaviour stays compatible.
- Run the gate before relation expansion and preserve bounded evidence reached from an admitted anchor.
- Use only aggregate, content-free diagnostics.

## Review Focus

- A repeated option or option identical to the question must not consume another variant slot; Task 1 pins de-duplication.
- More than eight combined analyzer/option variants must be deterministically capped; Task 1 pins the exact cap.
- A memory appearing under several variants must contribute only once to a logical RRF channel; Task 2 pins channel merging.
- A metadata-only or low-vector nearest neighbour must not seed relation expansion; Task 3 pins gate order.
- Exact-score message/context ties must prefer the message without altering either score; Task 3 pins tie behaviour.

---

### Task 1: Bounded option-aware query variants

**Files:**
- Modify: `src/masm/retrieval/query_analyzer.py`
- Modify: `src/masm/services/search_service.py`
- Test: `tests/unit/retrieval/test_query_analyzer.py`
- Create: `tests/unit/services/test_search_service.py`

**Interfaces:**
- Consumes: `ParsedQuery` and `SearchRequest.query/options`.
- Produces: `with_option_variants(parsed: ParsedQuery, query: str | Sequence[ContentPart], options: Sequence[str] | None, *, max_variants: int = 8) -> ParsedQuery`.

- [ ] **Step 1: Write failing tests for option symmetry, normalized de-duplication, multimodal text extraction, and the eight-variant cap**

Assert that existing analyzer queries stay first, every distinct option adds `<question>\nCandidate: <option>`, image bytes are preserved, and normalized duplicates disappear.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_query_analyzer.py tests/unit/services/test_search_service.py -q`

Expected: FAIL because `with_option_variants` does not exist and Search ignores options.

- [ ] **Step 3: Implement the query-variant helper and wire it into `SearchService._analyze`**

Use a stable case-folded whitespace-normalized de-duplication key. Keep `ParsedQuery` metadata and visual queries unchanged.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/unit/retrieval/test_query_analyzer.py tests/unit/services/test_search_service.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/masm/retrieval/query_analyzer.py src/masm/services/search_service.py tests/unit/retrieval/test_query_analyzer.py tests/unit/services/test_search_service.py
git commit -m "feat: add bounded option-aware search variants"
```

### Task 2: Independent recall and raw signal preservation

**Files:**
- Modify: `src/masm/storage/types.py`
- Modify: `src/masm/retrieval/baseline.py`
- Test: `tests/unit/retrieval/test_baseline_recall.py`

**Interfaces:**
- Consumes: Task 1's bounded `ParsedQuery.text_queries`.
- Produces: `MemoryCandidate.retrieval_signals: Mapping[str, float]` and `_merge_channels(channels: Sequence[RankedChannel]) -> list[RankedChannel]`.

- [ ] **Step 1: Write failing tests for independent lexical/vector calls, batched embeddings, per-channel de-duplication, and raw signal preservation**

Assert that two variants cause two lexical calls but produce one logical lexical channel, one text embedding batch, one RRF contribution per logical channel, and best raw scores in `retrieval_signals`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_baseline_recall.py -q`

Expected: FAIL because recall still concatenates queries and candidates have no signals.

- [ ] **Step 3: Implement independent recall, logical-channel merging, and signal propagation**

Embed all non-empty text variants in one call; create recall tasks per variant; merge after deterministic task collection; leave RRF weights and public scores unchanged.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/unit/retrieval/test_baseline_recall.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/masm/storage/types.py src/masm/retrieval/baseline.py tests/unit/retrieval/test_baseline_recall.py
git commit -m "feat: preserve raw retrieval signals across query variants"
```

### Task 3: Strong-anchor relevance gate and score-safe message tie-break

**Files:**
- Create: `src/masm/retrieval/relevance.py`
- Modify: `src/masm/config.py`
- Modify: `src/masm/runtime.py`
- Modify: `src/masm/api/app.py`
- Modify: `src/masm/services/search_service.py`
- Modify: `src/masm/retrieval/reranker.py`
- Modify: `.env.example`
- Test: `tests/unit/retrieval/test_relevance.py`
- Test: `tests/unit/retrieval/test_conflict_reranking.py`
- Test: `tests/unit/test_runtime_factory.py`
- Test: `tests/unit/test_runtime_settings.py`
- Test: `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Consumes: Task 2's `MemoryCandidate.retrieval_signals`.
- Produces: `RelevanceGate(min_text_similarity: float = 0.48, min_image_similarity: float = 0.42)` with `filter(candidates: Sequence[MemoryCandidate]) -> list[MemoryCandidate]`; `RuntimeComponents.relevance_gate: RelevanceGate | None`.

- [ ] **Step 1: Write failing unit tests for threshold validation, lexical/vector admission, metadata/weak-vector rejection, and exact-score message tie-breaking**

Assert thresholds must be finite in `[-1, 1]`, lexical presence admits even a low rank, vector scores use the configured cutoffs, and a message wins only when the final score is exactly tied.

- [ ] **Step 2: Write failing integration tests for empty abstention and gate-before-expansion**

Use an injected gate and deterministic candidates: unrelated weak history returns `data=[]`; a rejected candidate cannot pull a related neighbour; an admitted lexical anchor can.

- [ ] **Step 3: Run the focused tests and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_relevance.py tests/unit/retrieval/test_conflict_reranking.py tests/unit/test_runtime_factory.py tests/unit/test_runtime_settings.py tests/integration/test_retrieval_enhancements.py -q`

Expected: FAIL because the gate/runtime fields do not exist and the fixed message bonus changes scores.

- [ ] **Step 4: Implement the gate, official runtime wiring, environment settings, and score-safe tie-break**

Call the gate after user filtering and before `_with_expansion`. Remove `_MESSAGE_BONUS` from admission/scoring and sort exact ties by granularity then memory id. Copy retrieval signals into `RankedEvidence.metadata`.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/unit/retrieval/test_relevance.py tests/unit/retrieval/test_conflict_reranking.py tests/unit/test_runtime_factory.py tests/unit/test_runtime_settings.py tests/integration/test_retrieval_enhancements.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Run full quality gates**

Run: `python -m pytest -q`

Expected: `638+ passed`, with only the existing platform-specific skip.

Run: `ruff check .`

Expected: no errors.

Run: `mypy src scripts/cloud_smoke_recovery.py scripts/delete_evaluation_runs.py`

Expected: no errors.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add .env.example src/masm/config.py src/masm/runtime.py src/masm/api/app.py src/masm/services/search_service.py src/masm/retrieval/relevance.py src/masm/retrieval/reranker.py tests/unit/retrieval/test_relevance.py tests/unit/retrieval/test_conflict_reranking.py tests/unit/test_runtime_factory.py tests/unit/test_runtime_settings.py tests/integration/test_retrieval_enhancements.py
git commit -m "feat: abstain when retrieval has no strong anchor"
```
