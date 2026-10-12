# MASM v1.1 Add-Side Relation Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist high-precision cross-session relation links during Add so the existing Search relation expansion can retrieve multi-session evidence without regressing direct recall.

**Architecture:** Build an eight-item, same-user history pool from bounded semantic variants plus a two-item recency tail. A dedicated structured linker proposes at most four non-destructive links, and a deterministic validator admits only supplied targets with two grounded quotes and confidence at least `0.85`. Search and selector code remain unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy/PostgreSQL with pgvector, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-12-masm-v11-add-side-relation-materialization-design.md`

## Global Constraints

- Do not add or change question-anchor validation or require Search identifiers in evidence.
- Do not infer connectivity when persisted relation edges are absent.
- Do not modify Search production code, selector behavior, RelationExpander, public schemas, or response limits.
- Add at most one LLM call to an eligible historical Add and no Search model call.
- Keep `MAX_HISTORY = 8`, reserve six semantic positions and two recent positions, cap output at four links, and freeze `MIN_LINK_CONFIDENCE = 0.85`.
- New links are non-destructive `relation_type="link"`; existing Curator governance wins on duplicate targets.
- Linker failure yields no new links but preserves the otherwise valid Add result.
- Add no database migration, dependency, backfill, raw-content logging, or hidden-evaluation fixture.
- Do not deploy, touch cloud PostgreSQL or routing, run official Smoke, or run Full evaluation.

## Review Focus

- Repeated whitespace and Unicode evidence: whitespace-only normalization must accept exact source text while case or character changes remain ungrounded; Task 2 pins this.
- Semantic/recent overlap at the eight-item boundary: IDs must be unique and semantic order stable while unused recent slots return to semantic candidates; Task 3 pins this.
- Linker timeout/provider/schema failure after successful perception and curation: Add must keep the structured memory and Curator actions instead of degrading; Task 4 pins this.
- A Curator relation and linker proposal targeting the same memory: exactly one Curator-owned relation must persist; Task 2 and Task 4 pin this.
- Image-only or blank-text historical Adds: the new linker must not receive image bytes or make a model call; Task 4 pins this.

---

### Task 1: Strict relation-linker output and agent

**Files:**
- Create: `src/masm/agents/relation_linker.py`
- Create: `src/masm/agents/prompts/relation_linker_v1.txt`
- Modify: `src/masm/schemas/agents.py`
- Create: `tests/unit/agents/test_relation_linker_agent.py`
- Modify: `tests/unit/agents/test_agent_schemas.py`

**Interfaces:**
- Consumes: existing `StructuredLLM`, `ModelRequest`, `MemoryCandidate`, and `MemoryDraft`.
- Produces: `RelationLinkProposal(target_memory_id: UUID, current_evidence: str, target_evidence: str, confidence: Confidence)` and `RelationLinkDecision(links: tuple[RelationLinkProposal, ...])` in `masm.schemas.agents`.
- Produces: `RelationLinkerAgent.propose(new_memory: MemoryDraft, history: Sequence[MemoryCandidate]) -> RelationLinkDecision`, `prompt_version == "relation-linker-v1"`, and `max_history` bounded through `resolve_max_history`.

- [ ] **Step 1: Write failing schema and agent tests**

Add tests named:

- `test_relation_link_proposal_requires_two_non_blank_evidence_quotes`
- `test_relation_link_decision_rejects_more_than_four_links`
- `test_relation_linker_sends_original_text_and_bounded_candidate_ids`
- `test_relation_linker_records_model_and_prompt_version`
- `test_relation_linker_prompt_requires_factual_connection_and_forbids_topic_only_links`
- `test_relation_linker_prompt_contains_no_benchmark_examples`

Assert that the payload contains the current immutable `original_text`, at most eight supplied
candidate IDs/content values, and no repository object or Search query.

- [ ] **Step 2: Run the tests and observe RED**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/unit/agents/test_relation_linker_agent.py tests/unit/agents/test_agent_schemas.py -q`

Expected: collection/import failure because the schemas and agent do not exist.

- [ ] **Step 3: Implement the strict output models, prompt, and agent**

Keep `RelationLinkDecision.links` at a schema maximum of four. Follow the existing Curator agent
shape: load a packaged prompt, clamp history at eight, send exact IDs/content, and return only the
validated structured decision.

- [ ] **Step 4: Run focused agent tests GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/masm/agents/relation_linker.py src/masm/agents/prompts/relation_linker_v1.txt src/masm/schemas/agents.py tests/unit/agents/test_relation_linker_agent.py tests/unit/agents/test_agent_schemas.py
git commit -m "feat: add bounded relation linker agent"
```

### Task 2: Deterministic relation-link validation and merge

**Files:**
- Create: `src/masm/orchestration/relation_link_validator.py`
- Create: `tests/unit/orchestration/test_relation_link_validator.py`

**Interfaces:**
- Consumes: Task 1 `RelationLinkDecision`; existing `MemoryCandidate`, `RelationDraft`, and `ValidatedActions`.
- Produces: `MIN_LINK_CONFIDENCE = 0.85`, `MAX_LINKS = 4`.
- Produces: `validate_relation_links(user_id: str, decision: RelationLinkDecision, current_text: str, candidates: Sequence[MemoryCandidate], *, min_confidence: float = MIN_LINK_CONFIDENCE, max_links: int = MAX_LINKS) -> tuple[RelationDraft, ...]`.
- Produces: `merge_validated_actions(actions: ValidatedActions, links: Sequence[RelationDraft]) -> ValidatedActions`.

- [ ] **Step 1: Write failing validator tests**

Add tests named:

- `test_accepts_grounded_same_user_context_link_at_frozen_threshold`
- `test_accepts_quotes_after_whitespace_normalization_only`
- `test_rejects_case_or_character_changed_quote`
- `test_ignores_foreign_unknown_and_message_targets`
- `test_ignores_low_confidence_duplicate_and_over_cap_proposals`
- `test_relation_evidence_ref_contains_both_grounded_quotes`
- `test_curator_relation_wins_when_linker_targets_the_same_memory`
- `test_merge_preserves_duplicate_supersede_conflict_fields`

Use paraphrased records that share no literal identifier for the positive case; the quotes ground
each record independently and must not be compared with a Search question.

- [ ] **Step 2: Run the validator tests and observe RED**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/unit/orchestration/test_relation_link_validator.py -q`

Expected: import failure because the validator module does not exist.

- [ ] **Step 3: Implement validation and action merging**

Normalize quotes with `" ".join(value.split())` only. Validate the configured bounds, build a
candidate map from same-user context memories, ignore invalid individual proposals, and stop after
four accepted unique targets. Store both exact model-returned quotes in `evidence_ref`. When merging,
retain every Curator field and relation, adding only linker targets not already present.

- [ ] **Step 4: Run focused validator tests GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/masm/orchestration/relation_link_validator.py tests/unit/orchestration/test_relation_link_validator.py
git commit -m "feat: validate grounded relation links"
```

### Task 3: Bounded multi-view history candidates

**Files:**
- Create: `src/masm/orchestration/history_candidates.py`
- Create: `tests/unit/orchestration/test_history_candidates.py`

**Interfaces:**
- Consumes: existing `AddRequest`, `PerceptionResult`, `ParsedQuery`, `BaselineRetriever`, and repository `metadata_candidates`.
- Produces: `SEMANTIC_SLOTS = 6`, `RECENT_SLOTS = 2`, `MAX_TEXT_QUERIES = 4`.
- Produces: `HistoryCandidateBuilder.__init__(retriever: BaselineRetriever, *, max_history: int = MAX_HISTORY)` and `build(request: AddRequest, perception: PerceptionResult) -> list[MemoryCandidate]`.

- [ ] **Step 1: Write failing candidate-builder tests**

Add tests named:

- `test_builds_stable_raw_entity_keyword_and_combined_queries`
- `test_batches_at_most_four_text_variants_and_supplies_metadata_entities`
- `test_reserves_two_recent_contexts_and_deduplicates_semantic_overlap`
- `test_unused_recent_slots_return_to_semantic_candidates_in_order`
- `test_history_never_exceeds_eight_and_contains_only_same_user_contexts`
- `test_zero_history_and_user_without_context_skip_embeddings_and_recent_query`
- `test_other_user_history_does_not_trigger_recall`

The boundary test must use more than eight candidates and overlap the semantic and recent sets.

- [ ] **Step 2: Run the candidate tests and observe RED**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/unit/orchestration/test_history_candidates.py -q`

Expected: import failure because `HistoryCandidateBuilder` does not exist.

- [ ] **Step 3: Implement the bounded builder**

Check `max_history` and `repository.has_context_memory(user_id)` before embedding. Create stable,
non-empty, de-duplicated variants, call hybrid retrieval with `granularity="context"`, and call the
same-user metadata query for the recent tail. Filter defensively by user and granularity before the
six-plus-two merge and fill unused positions from remaining semantic results.

- [ ] **Step 4: Run focused candidate tests GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/masm/orchestration/history_candidates.py tests/unit/orchestration/test_history_candidates.py
git commit -m "feat: broaden bounded add history recall"
```

### Task 4: Integrate linking into Add and official runtime

**Files:**
- Modify: `src/masm/orchestration/add_pipeline.py`
- Modify: `src/masm/runtime.py`
- Modify: `tests/integration/test_agent_add_pipeline.py`
- Modify: `tests/unit/test_runtime_factory.py`

**Interfaces:**
- Consumes: Tasks 1-3 agent, validator, merger, and candidate builder.
- Produces: `PipelineState.RELATION_LINKED` between `CURATED` and `VALIDATED`.
- Produces: optional `relation_linker: RelationLinkerAgent | None` and `history_builder: HistoryCandidateBuilder | None` constructor dependencies on `AddPipeline`.
- Produces: official runtime wiring and `audit_metadata()["prompt_versions"]["relation_linker"] == "relation-linker-v1"`.

- [ ] **Step 1: Write failing pipeline/runtime tests**

Update the full state trace and add tests named:

- `test_history_add_calls_relation_linker_exactly_once_and_persists_valid_link`
- `test_first_add_and_max_history_zero_skip_relation_linker`
- `test_blank_text_and_image_only_history_adds_skip_relation_linker`
- `test_linker_provider_schema_and_quote_failures_preserve_structured_add`
- `test_curator_relation_prevents_duplicate_generic_link_persistence`
- `test_paraphrased_cross_session_records_link_without_shared_identifier`
- `test_three_add_chain_persists_two_adjacencies_and_isolated_add_stays_unlinked`
- `test_official_runtime_wires_relation_linker_and_reports_prompt_version`
- `test_non_official_profiles_do_not_wire_relation_linker`

For failure tests, assert `degraded is False`, the perception-derived keywords remain, existing
Curator fields remain, and no linker edge is persisted.

- [ ] **Step 2: Run focused pipeline/runtime tests and observe RED**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/integration/test_agent_add_pipeline.py tests/unit/test_runtime_factory.py -q`

Expected: failures for the missing state, constructor inputs, wiring, and persisted links.

- [ ] **Step 3: Integrate candidate building, optional linking, and action merge**

Build the draft once after temporal analysis, keep existing Curator behavior, call the linker only
for eligible text with history, catch expected provider/structured-output/validation failures as an
empty link decision, merge validated links, and advance every successful path through
`RELATION_LINKED`. Wire the agent only for `OFFICIAL_MASM` and add its prompt version to sanitized
audit metadata.

- [ ] **Step 4: Run focused pipeline/runtime tests GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Run all Add, relation, isolation, and paid-regression tests**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/integration/test_agent_add_pipeline.py tests/unit/orchestration tests/unit/agents tests/unit/storage/test_relations.py tests/isolation/test_relation_user_isolation.py tests/unit/retrieval/test_evidence_selector.py tests/unit/test_runtime_factory.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/masm/orchestration/add_pipeline.py src/masm/runtime.py tests/integration/test_agent_add_pipeline.py tests/unit/test_runtime_factory.py
git commit -m "feat: materialize validated add-time relations"
```

### Task 5: Strengthen the content-safe probe and verify the candidate

**Files:**
- Modify: `scripts/multi_session_path_probe.py`
- Modify: `tests/unit/test_multi_session_path_probe_script.py`
- Modify if required by typing only: `scripts/compare_search_diagnostics.py`

**Interfaces:**
- Consumes: persisted links produced by Task 4 and existing `MemoryRepository.related`.
- Produces: aggregate probe row `case="connected_relations"` with `expected_adjacency_count`, `matched_adjacency_count`, and `passed`; no content, identifiers, or relation quotes are printed.

- [ ] **Step 1: Write the failing probe tests**

Add tests named:

- `test_probe_requires_both_connected_chain_adjacencies`
- `test_probe_fails_when_only_one_connected_adjacency_exists`
- `test_probe_keeps_isolated_relation_check_independent`
- `test_probe_output_never_contains_generated_markers`

Use candidate IDs in the fake repository to count root↔bridge and bridge↔leaf adjacency without
inspecting marker text.

- [ ] **Step 2: Run the probe tests and observe RED**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/unit/test_multi_session_path_probe_script.py -q`

Expected: failure because the `connected_relations` row is absent.

- [ ] **Step 3: Add aggregate connected-relation diagnostics**

After loading stored contexts, query bounded neighbours for the three chain contexts, count only the
two expected ID adjacencies, emit the aggregate row, and keep the existing isolated-memory check.
Do not print marker text, memory IDs, evidence, or raw relation data.

- [ ] **Step 4: Run probe and focused regression tests GREEN**

Run: `& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest tests/unit/test_multi_session_path_probe_script.py tests/unit/test_search_path_probe_script.py tests/unit/retrieval/test_evidence_selector.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Run the complete local verification suite**

Run in order:

```powershell
& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m pytest -q
& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m ruff check .
& E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe -m mypy src scripts/selector_probe.py scripts/selector_reasoning_probe.py scripts/compare_search_diagnostics.py scripts/cloud_smoke_recovery.py scripts/multi_session_path_probe.py
git diff --check
```

Expected: pytest has zero failures and the one Windows-only skip; Ruff and mypy report no issues;
`git diff --check` exits `0`.

- [ ] **Step 6: Commit Task 5**

```powershell
git add scripts/multi_session_path_probe.py tests/unit/test_multi_session_path_probe_script.py scripts/compare_search_diagnostics.py
git commit -m "test: require persisted multi-session relations"
```

- [ ] **Step 7: Prepare final review evidence**

Record the exact branch SHA, clean status, focused RED→GREEN evidence, full verification summaries,
and the unchanged deployment/paid-evaluation boundary. Do not deploy or run any official evaluation.
