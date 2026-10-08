# MASM v1.1 Precision Evidence Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise v1.1 Search precision and evidence sufficiency so the next official Smoke run can target at least 50 while retaining atomic retrieval.

**Architecture:** Original-question recall feeds a strengthened relevance gate, relation expansion, and the existing deterministic reranker. A bounded source-diversified pool then goes through one structured `gpt-4o-mini` evidence-selection call; validated indices are rendered as original evidence, while failures use an anchor-only deterministic shortlist.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, existing OpenAI-compatible LLM client, SQLAlchemy/PostgreSQL, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-08-masm-v11-evidence-selection-design.md`

## Global Constraints

- The original question alone determines candidate admission; options are auxiliary selector context.
- At most one structured evidence-selection request is created per Search; set `ModelRequest.max_attempts=1` so the provider makes at most one HTTP attempt for that request.
- The selector sees at most 32 candidates and returns at most 12 original evidence items.
- The public Add/Search schemas, `top_k <= 100`, `user_id` isolation, and existing byte cap remain unchanged.
- A valid insufficient-evidence decision returns an empty successful Search response; provider or validation failure uses deterministic anchor-only fallback and never becomes a Search 503.
- Selector payloads contain canonical text only; original image bytes are restored after selection.
- No hidden evaluation content, raw identities, options, candidate text, keys, or provider bodies enter diagnostics.
- No new provider, model, migration, or heavy dependency is introduced.

## Review Focus

- A syntactically valid selector response with duplicate, negative, out-of-range, Boolean, or more than 12 indices falls back deterministically; Task 3 tests each case.
- A query with only images has no textual question; Task 3 tests the visual marker and candidate descriptions without passing image bytes to the selector.
- Many unique low-ranked sources could crowd out several strong facts from one source; Task 2 tests a half-budget diversity reservation followed by normal-rank fill.
- Relation-expanded items may lack recall signals; Task 3 tests that fallback excludes them while a successful selector can retain them.
- Selector error messages or model payloads may contain user text; Task 4 tests that Search diagnostics log only whitelisted counts, flags, and sanitized categories.

---

### Task 1: Question-only admission and lexical strength

**Files:**
- Modify: `src/masm/services/search_service.py` (`_analyze`)
- Modify: `src/masm/retrieval/query_analyzer.py` (retire `with_option_variants` and its now-unused cap)
- Modify: `src/masm/retrieval/relevance.py` (`RelevanceGate`)
- Modify: `src/masm/config.py` (lexical threshold setting)
- Modify: `.env.example` (threshold documentation)
- Test: `tests/unit/retrieval/test_query_analyzer.py`
- Test: `tests/unit/retrieval/test_relevance.py`
- Test: `tests/unit/test_runtime_settings.py`
- Test: `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Produces: `SearchService._analyze(request: SearchRequest) -> ParsedQuery` with only question-derived text/visual variants.
- Produces: `RelevanceGate(min_lexical_rank: float = 0.001, min_text_similarity: float = 0.48, min_image_similarity: float = 0.42)` and read-only `min_lexical_rank`.
- Produces: `Settings.min_lexical_rank: float = 0.001`, read from `MASM_MIN_LEXICAL_RANK`; validation requires a finite value in `(0, 1]`.

- [ ] **Step 1: Write failing tests**

Add `test_search_options_do_not_change_recall_query` using a recording retriever and assert that two requests with different options send identical `ParsedQuery.text_queries`; assert no `Candidate:` token enters recall. Replace the obsolete option-variant tests with one test that a complex original question still yields no more than three analyzer subqueries. Add `test_lexical_rank_requires_positive_threshold` asserting rank `0.0` and `0.0009` are rejected but `0.001` is admitted; retain strong text/image admission. Test invalid lexical configuration and environment parsing.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_query_analyzer.py tests/unit/retrieval/test_relevance.py tests/unit/test_runtime_settings.py tests/integration/test_retrieval_enhancements.py -q`

Expected: new option and lexical tests fail against existing behaviour.

- [ ] **Step 3: Implement question-only analysis and lexical threshold**

Delete `with_option_variants`; return analyzer output directly from `_analyze`. Require finite lexical rank `>= 0.001`, without changing text/image defaults. Wire the setting in Task 4's official runtime; do not change local-fake admission.

- [ ] **Step 4: Run the same focused tests and verify GREEN**

- [ ] **Step 5: Commit**

Run: `git add src/masm/services/search_service.py src/masm/retrieval/query_analyzer.py src/masm/retrieval/relevance.py src/masm/config.py .env.example tests/unit/retrieval/test_query_analyzer.py tests/unit/retrieval/test_relevance.py tests/unit/test_runtime_settings.py tests/integration/test_retrieval_enhancements.py`

Run: `git commit -m "fix: admit search evidence from question signals"`

### Task 2: Bounded, source-diversified selector pool

**Files:**
- Create: `src/masm/retrieval/selector_pool.py`
- Create: `tests/unit/retrieval/test_selector_pool.py`

**Interfaces:**
- Consumes: `RankedEvidence` ordered by the existing deterministic reranker.
- Produces: `build_selector_pool(ranked: Sequence[RankedEvidence], *, max_candidates: int = 32) -> list[RankedEvidence]`.
- Rule: reserve at most `max_candidates // 2` first occurrences of distinct nonempty `request_id` values in rank order, then fill by overall rank, without duplicate `memory_id`; items with no `request_id` participate only in fill.

- [ ] **Step 1: Write failing pool tests**

Test a high-ranked source with several necessary facts plus 40 unique lower-ranked sources: the 32-item pool includes at least two facts from the high-ranked source and at least two distinct sources. Test empty pool, zero limit, stable order, blank `request_id`, duplicate memory IDs, and a caller limit below 32.

- [ ] **Step 2: Run tests and verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_selector_pool.py -q`

Expected: import fails because the pool module does not exist.

- [ ] **Step 3: Implement `build_selector_pool`**

Keep reservation and fill deterministic; return items in their original relative rank order after selection. Reject a negative limit and clamp limits above 32.

- [ ] **Step 4: Run pool tests and verify GREEN**

- [ ] **Step 5: Commit**

Run: `git add src/masm/retrieval/selector_pool.py tests/unit/retrieval/test_selector_pool.py`

Run: `git commit -m "feat: diversify bounded evidence candidates"`

### Task 3: Structured evidence selection and safe fallback

**Files:**
- Create: `src/masm/retrieval/evidence_selector.py`
- Create: `tests/unit/retrieval/test_evidence_selector.py`

**Interfaces:**
- Consumes: Task 2 `build_selector_pool`, existing `StructuredLLM.complete_json`, `ModelRequest`, `RankedEvidence`, and caller-supplied strong-anchor IDs.
- Produces: `EvidenceSelection(BaseModel)` with strict `selected_indices: tuple[StrictInt, ...]` and `sufficient_evidence: bool`; reject extra fields.
- Produces: `SelectionResult` with `evidence: tuple[RankedEvidence, ...]`, `candidate_count: int`, `source_count: int`, `selected_source_count: int`, `fallback: bool`, `abstained: bool`, and `failure_category: Literal["none", "unavailable", "invalid_output"]`.
- Produces: `EvidenceSelector.select(query: str | Sequence[ContentPart], options: Sequence[str] | None, ranked: Sequence[RankedEvidence], strong_anchor_ids: Collection[UUID]) -> SelectionResult`.
- Produces: `DeterministicEvidenceSelector.select(...) -> SelectionResult` with the same signature; its local-fake passthrough returns ranked evidence in order, leaving the caller's `top_k` intact and making no model call.
- Constructor bounds: `max_candidates=32`, `max_selected=12`, `max_chars_per_candidate=1200`, `timeout_seconds=30.0`; request `max_attempts=1`, prompt version `evidence-selector-v1`, existing configured model.

- [ ] **Step 1: Write failing selector tests with `FakeStructuredLLM`**

Assert direct fact selection returns its original object; an insufficient decision returns empty; a join can select two different `request_id` values; options appear in the model payload only; canonical descriptions are truncated to 1200 characters; image bytes never enter the payload; an image-only query uses a bounded visual marker. Assert exactly one model request for a nonempty pool and zero for an empty pool.

- [ ] **Step 2: Run selector tests and verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_evidence_selector.py -q`

Expected: import fails because the selector module does not exist.

- [ ] **Step 3: Implement schema, prompt, pool serialization, and selection**

Send only opaque source-group labels, source position, granularity, canonical text, and bounded numeric retrieval signals. Preserve selector index order, reject duplicates/out-of-range/over-limit values, and return no evidence if `sufficient_evidence=False`.

- [ ] **Step 4: Add failing fault and fallback tests**

Cover provider exception, timeout, schema error, Boolean/negative/out-of-range/duplicate/over-limit index, empty-but-sufficient output, relation-expanded item without anchor ID, and no strong anchors. Assert fallback uses at most 12 question-admitted items and stays source-diversified. Assert a valid insufficient decision never invokes fallback.

- [ ] **Step 5: Implement deterministic anchor-only fallback and run GREEN**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_evidence_selector.py tests/unit/retrieval/test_selector_pool.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Run: `git add src/masm/retrieval/evidence_selector.py tests/unit/retrieval/test_evidence_selector.py`

Run: `git commit -m "feat: select sufficient search evidence"`

### Task 4: Search integration, runtime configuration, and private diagnostics

**Files:**
- Modify: `src/masm/services/search_service.py`
- Modify: `src/masm/retrieval/diagnostics.py`
- Modify: `src/masm/config.py`
- Modify: `src/masm/runtime.py`
- Modify: `src/masm/api/app.py`
- Modify: `deployments/docker-compose.v11.yml`
- Modify: `.env.example`
- Test: `tests/unit/retrieval/test_diagnostics.py`
- Test: `tests/unit/test_runtime_settings.py`
- Test: `tests/unit/test_runtime_factory.py`
- Test: `tests/integration/test_runtime_profiles.py`
- Test: `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Consumes: Task 3 `EvidenceSelector.select` and its `SelectionResult`.
- Produces: optional `selector: EvidenceSelector | DeterministicEvidenceSelector | None` in `SearchService.__init__`; official MASM injects the real selector when enabled, local-fake injects the deterministic passthrough, and official-baseline preserves its existing path. Tests inject `FakeStructuredLLM` into the real selector.
- Produces: `Settings.selector_enabled: bool = True`, `selector_max_candidates: int = 32`, `selector_max_selected: int = 12`, `selector_max_chars_per_candidate: int = 1200`, `selector_timeout_seconds: float = 30.0`; official wiring only. Validate positive bounds, `max_selected <= max_candidates`, and selector timeout `<= model_timeout_seconds`.
- Produces: aggregate `SearchDiagnostics` selector counts, distinct-source counts, fallback/abstention flags, and a sanitized failure category; all new fields default to neutral values for old call sites.

- [ ] **Step 1: Write failing service and official-app tests**

Assert question-only recall feeds a selector after ranking but before renderer; the selected subset is rendered in selector order, packed under both `top_k` and 12, and an empty selected set yields `{"data": []}` with HTTP 200. Add a real Add-to-Search synthetic test for direct evidence, two-source comparison, unrelated entity overlap, and original text-image-text return. Update the existing official-runtime fake LLM fixture to queue one selection response and expect one new `EvidenceSelection` record; keep local-fake top-100 behaviour intact through the passthrough selector.

- [ ] **Step 2: Run focused integration tests and verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_runtime_profiles.py tests/integration/test_retrieval_enhancements.py tests/unit/test_runtime_factory.py -q`

Expected: new selector wiring assertions fail.

- [ ] **Step 3: Integrate selector and official runtime configuration**

Capture strong-anchor IDs immediately after the relevance gate. Select from reranked evidence before rendering. Wire settings through `build_runtime` and `create_app`; expose the new bounds in Compose and `.env.example`. Use the injected fake LLM for unit/integration tests and the already configured model in production.

- [ ] **Step 4: Write failing configuration and privacy tests**

Assert invalid selector bounds/timeouts are rejected, official enable/disable flags work, and logs contain only whitelisted aggregate fields even when query, option, source ID, candidate text, or provider exception includes a sentinel secret. Assert selected/fallback/abstention/source counts and sanitized failure category in diagnostics. Preserve the existing provider call record's model and latency fields; token usage is recorded only if the provider exposes it, never inferred from content.

- [ ] **Step 5: Implement whitelisted diagnostics and run GREEN**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_diagnostics.py tests/unit/test_runtime_settings.py tests/unit/test_runtime_factory.py tests/integration/test_runtime_profiles.py tests/integration/test_retrieval_enhancements.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Run: `git add src/masm/services/search_service.py src/masm/retrieval/diagnostics.py src/masm/config.py src/masm/runtime.py src/masm/api/app.py deployments/docker-compose.v11.yml .env.example tests/unit/retrieval/test_diagnostics.py tests/unit/test_runtime_settings.py tests/unit/test_runtime_factory.py tests/integration/test_runtime_profiles.py tests/integration/test_retrieval_enhancements.py`

Run: `git commit -m "feat: wire evidence selector into official search"`

### Task 5: Regression, release evidence, and deployment handoff

**Files:**
- Modify: `README.md` (Search privacy, extra call, and configuration)
- Modify: `docs/competition/masm-v11-release-checklist.md` (immutable-image release and rollback checklist)
- Test: `tests/unit/retrieval/test_evidence_selector.py`
- Test: `tests/integration/test_runtime_profiles.py`
- Test: `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Consumes: Tasks 1–4 completed commits.
- Produces: a reviewed candidate commit, exact deployment commands for the existing VNC/manual-input workflow, and content-free verification/cleanup commands. No database migration.

- [ ] **Step 1: Add targeted regression cases for previous atomic and abstention behaviour**

Assert atomic evidence survives the 12-item cap when the selector chooses it; option-only and same-entity irrelevant memories abstain; a provider failure returns HTTP 200 and only admitted anchors; a multi-source question preserves both original source messages.

- [ ] **Step 2: Run targeted tests and verify RED for any uncovered behaviour**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_evidence_selector.py tests/integration/test_runtime_profiles.py tests/integration/test_retrieval_enhancements.py -q`

- [ ] **Step 3: Make only the minimal fixes required by those tests and rerun GREEN**

- [ ] **Step 4: Run complete local gates**

Run: `..\..\.venv\Scripts\python.exe -m pytest -q`

Expected: zero failures; record passed/skipped/warnings rather than assuming an old count.

Run: `..\..\.venv\Scripts\ruff.exe check .`

Expected: zero errors.

Run: `..\..\.venv\Scripts\mypy.exe src scripts/cloud_smoke_recovery.py scripts/delete_evaluation_run.py`

Expected: zero errors.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 5: Review the entire branch against the spec and document the release procedure**

Check source commit/tree, model/call cap, provider payload privacy, DB isolation, original evidence rendering, HTTP 200 fallback, no migration, and rollback image. Prepare exact manual server commands but do not include keys or hidden evaluation content.

- [ ] **Step 6: Commit and hand off the candidate**

Run: `git add README.md docs/competition/masm-v11-release-checklist.md tests/unit/retrieval/test_evidence_selector.py tests/integration/test_runtime_profiles.py tests/integration/test_retrieval_enhancements.py`

Run: `git commit -m "docs: prepare precision search candidate release"`

After the user deploys by VNC, verify immutable image ID, healthy API, unchanged DB container ID, internal/external health, one content-free positive/abstention/fallback probe, and zero residual probe rows/assets. Then run one official v1.1 Smoke and compare only public aggregate scores. If the score remains below 50, diagnose from aggregate evidence without inspecting hidden questions.
