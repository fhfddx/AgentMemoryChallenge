# MASM v1.1 Evidence Graph Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the reviewed evidence-graph branch into a locally verified API-only canary candidate by fixing the reproduced correctness, isolation, boundedness, and diagnostic-leak gaps.

**Architecture:** Keep the planner proposal narrow and treat stored provenance as deterministic validator/assembler input. Scope and bound one candidate pool before any provider call, derive graph state from fully enumerated bounded evidence, and keep provenance queries bounded in SQL.

**Tech Stack:** Python 3.14, Pydantic v2, SQLAlchemy 2, PostgreSQL/pgvector, pytest, Ruff, mypy

**Spec:** `docs/superpowers/specs/2026-10-15-masm-v11-evidence-graph-review-fixes-design.md`

## Global Constraints

- Do not change public Add/Search schemas, the database schema, default-off rollout, frozen-v7 success behavior, rendering, multimodal routing, local-fake behavior, response budgets, deletion, or concurrency semantics.
- Do not access online APIs/databases, provider endpoints, deployment targets, or evaluation pages; do not run Smoke/Full and do not push.
- Never read, display, copy, move, delete, stage, or commit `E:\Competitions\AgentMemoryChallenge\秘钥.txt`, `.env`, provider bodies, raw production logs, or hidden evaluation content.
- Database tests may use only the verified local `masm_test`; stop only a container started for this plan.
- Each behavioral fix follows RED-GREEN and lands in its own commit.

## Review Focus

- A foreign candidate at rank one must not choose the effective user scope or enter planner/fallback payloads (Task 1).
- More than 32 ranked candidates plus planner failure must not trigger frozen v7's deep retry (Task 2).
- A valid connected interpretation may occur after the former 512-combination cutoff (Task 3).
- Truncating a long connected path must not return disconnected endpoints (Task 4).
- Stored conflict peers and high-degree provenance frontiers must stay grounded, user-isolated, and bounded (Tasks 5 and 6).

---

### Task 1: Enforce explicit request-user scope

**Files:**
- Modify: `src/masm/retrieval/evidence_graph.py:338-452`
- Modify: `src/masm/retrieval/evidence_graph_rules.py:274-360`
- Modify: `src/masm/services/search_service.py:99-105`
- Modify: `scripts/evidence_graph_probe.py:678-684`
- Test: `tests/unit/retrieval/test_evidence_graph.py`

**Interfaces:**
- Consumes: `SearchRequest.user_id` and `RankedEvidence.user_id`.
- Produces: `GraphEvidenceSelector.select(..., *, request_user_id: str)` and `validate_proposal(..., expected_user_id: str)`; all planner and fallback inputs are scoped.

- [ ] **Step 1: Write the failing tests**

Add `test_request_user_scope_is_not_inferred_from_first_ranked_candidate` and a validator-level mixed-user rejection test. Assert the foreign rank-one candidate is absent from planner payload and selected evidence.

- [ ] **Step 2: Run the two tests and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "request_user_scope or validator_rejects_foreign" -q`

Expected: FAIL because the selector currently scopes to `ranked[0].user_id` and validation has no expected user.

- [ ] **Step 3: Implement the explicit scope**

Add the required keyword-only selector argument, filter before `build_selector_pool`, pass the expected user to validation, and update production/probe/test callers.

- [ ] **Step 4: Verify GREEN and the focused file**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py tests/unit/retrieval/test_evidence_graph_rules.py tests/unit/retrieval/test_evidence_graph_wiring.py tests/unit/test_evidence_graph_probe_script.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Commit: `fix: enforce evidence graph request user scope`

### Task 2: Limit graph failure to one v7 call

**Files:**
- Modify: `src/masm/retrieval/evidence_graph.py:372-452`
- Test: `tests/unit/retrieval/test_evidence_graph.py`

**Interfaces:**
- Consumes: Task 1's user-scoped `pool`.
- Produces: all graph failure branches invoke frozen v7 exactly once with that pool.

- [ ] **Step 1: Write the failing test**

Add `test_planner_failure_falls_back_once_over_the_same_bounded_pool` with 40 ranked candidates. Assert request prompt versions are exactly graph-v1 then selector-v7 and no third request occurs.

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py::test_planner_failure_falls_back_once_over_the_same_bounded_pool -q`

Expected: FAIL with three provider requests because v7 deep-retries over the unbounded ranked list.

- [ ] **Step 3: Pass the scoped bounded pool to every v7 fallback branch**

Every planner-failure branch uses the scoped bounded pool. Pure-image routing is not a planner failure and preserves the frozen-v7 path over the complete user-scoped ranked input. Preserve valid-insufficient no-fallback behavior.

- [ ] **Step 4: Verify GREEN and fallback regressions**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "fallback or insufficient or image" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Commit: `fix: bound evidence graph fallback to one v7 call`

### Task 3: Enumerate every valid bounded support combination

**Files:**
- Modify: `src/masm/retrieval/evidence_graph_assembly.py:25,191-203`
- Test: `tests/unit/retrieval/test_evidence_graph.py`

**Interfaces:**
- Consumes: at most four requirements and 24 validated supports.
- Produces: `_combinations(...)` enumerates the complete Cartesian product, at most 1296 choices.

- [ ] **Step 1: Write the failing test**

Add `test_connected_solution_after_512_combinations_is_sufficient` using four requirements with five supports each, where only the last support for every requirement is connected.

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py::test_connected_solution_after_512_combinations_is_sufficient -q`

Expected: FAIL with `partial` under the first-choice fallback.

- [ ] **Step 3: Remove the 512 shortcut and enumerate the capped product**

Keep deterministic product order and existing scoring.

- [ ] **Step 4: Verify GREEN and assembly tests**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "combination or connected or graph_cap" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Commit: `fix: enumerate bounded evidence graph combinations`

### Task 4: Keep over-budget partial evidence connected

**Files:**
- Modify: `src/masm/retrieval/evidence_graph_assembly.py:83-119,314-327`
- Test: `tests/unit/retrieval/test_evidence_graph.py`

**Interfaces:**
- Consumes: assembly adjacency, covering supports, and `max_selected`.
- Produces: `_best_bounded_partial(...) -> set[int]` whose induced selection is connected and covers the best feasible requirement subset.

- [ ] **Step 1: Tighten the long-chain test to require connectivity**

Rename/extend the existing cap test so a 14-node chain with a 12-node cap cannot return both disconnected endpoints; independently verify every selected node is reachable within the selected subgraph.

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "over_budget_partial" -q`

Expected: FAIL because `_bounded` preserves endpoints and drops bridge nodes.

- [ ] **Step 3: Implement deterministic connected partial selection**

Enumerate non-empty covered-requirement subsets and covering choices; retain same-component path unions that fit the cap and score by covered requirements, direct support, source diversity, size, then rank.

- [ ] **Step 4: Verify GREEN and cap regressions**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "cap or partial or connected" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Commit: `fix: keep bounded partial evidence connected`

### Task 5: Connect stored conflict peers internally

**Files:**
- Modify: `src/masm/retrieval/evidence_graph_assembly.py:40-188`
- Test: `tests/unit/retrieval/test_evidence_graph.py`

**Interfaces:**
- Consumes: validated direct/negative supports and candidate `conflict_group_id`.
- Produces: internal adjacency pairs for opposite roles in the same non-null group; planner link schema remains unchanged.

- [ ] **Step 1: Write the failing test**

Add `test_stored_conflict_group_connects_both_sides_without_planner_link`. Assert both sides are selected and state is `sufficient`; also assert the planner proposal contains no links.

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py::test_stored_conflict_group_connects_both_sides_without_planner_link -q`

Expected: FAIL because assembly chooses only one disconnected side.

- [ ] **Step 3: Add deterministic internal conflict adjacency**

Connect only grounded supports for the same requirement, opposite roles, and equal non-null stored group. Do not add a planner `LinkKind` or diagnostic link count.

- [ ] **Step 4: Verify GREEN and conflict regressions**

Run: `python -m pytest tests/unit/retrieval/test_evidence_graph.py -k "conflict or negative" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Commit: `fix: preserve both sides of stored conflicts`

### Task 6: Bound provenance relations in SQL

**Files:**
- Modify: `src/masm/storage/repositories.py:976-1033`
- Modify: `src/masm/retrieval/relation_expander.py:79-178`
- Modify: `tests/unit/retrieval/test_relation_expander_provenance.py`
- Modify: `tests/integration/test_evidence_graph_pipeline.py`

**Interfaces:**
- Consumes: the complete traversed endpoint set supplied to `relation_edges(user_id, memory_ids, limit)`.
- Produces: SQL-user-isolated, two-endpoint-filtered, ordered, limited `RelationEdge` rows; `expand()` remains byte-for-byte behaviorally unchanged.

- [ ] **Step 1: Write failing unit and integration tests**

Make the provenance repository double honor the two-endpoint contract, require the second-hop call to include bridge and discovered endpoint, and add a high-degree real-repository case where an in-frontier edge survives a small limit while outside-frontier incident edges do not.

- [ ] **Step 2: Run them and verify RED**

Run: `python -m pytest tests/unit/retrieval/test_relation_expander_provenance.py tests/integration/test_evidence_graph_pipeline.py -k "provenance or relation_sidecar" -q`

Expected: FAIL because the second-hop call omits the discovered endpoint and repository limiting happens after an unbounded incident-edge fetch.

- [ ] **Step 3: Implement SQL-bounded two-endpoint provenance**

Use two owned-memory aliases or equivalent SQL joins, require both relation endpoints in the supplied IDs, order and limit in SQL, and pass traversal bridge plus second-hop IDs from the expander.

- [ ] **Step 4: Verify GREEN plus legacy expander invariants**

Run: `python -m pytest tests/unit/retrieval/test_relation_expander.py tests/unit/retrieval/test_relation_expander_provenance.py tests/unit/storage/test_relations.py tests/isolation/test_relation_user_isolation.py tests/integration/test_evidence_graph_pipeline.py -q`

Expected: all selected tests pass and old `expand()` ordering/caps remain unchanged.

- [ ] **Step 5: Commit**

Commit: `fix: bound relation provenance in SQL`

### Task 7: Redact SQL bound parameters and close the handoff

**Files:**
- Modify: `src/masm/storage/db.py:14-17`
- Modify: `tests/unit/storage/test_database_safety.py`
- Modify: `docs/competition/masm-v11-evidence-graph-implementation-handoff-2026-10-15.md`

**Interfaces:**
- Consumes: all engine creation through `Database.create(database_url)`.
- Produces: SQLAlchemy exceptions/logs hide bound values; handoff reports actual commits and validation only.

- [ ] **Step 1: Write the failing redaction test**

Execute a deliberately invalid parameterized statement through a `Database.create` engine and assert the exception string omits a unique bound-value canary.

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest tests/unit/storage/test_database_safety.py -q`

Expected: FAIL because SQLAlchemy currently renders parameter values.

- [ ] **Step 3: Enable `hide_parameters=True`**

Keep pool, URL, and session behavior unchanged.

- [ ] **Step 4: Verify focused and full gates, then update handoff**

Run focused evidence-graph/storage suites, full `python -m pytest -q`, `python -m ruff check .`, `python -m mypy src`, and `git diff --check 6ad49ad3e99b725821e5e9d1b053d2a0148fe319..HEAD`. Record exact results and container state without secrets or raw provider bodies.

- [ ] **Step 5: Commit**

Commit: `fix: redact SQL parameters from failures`, then a separate `docs: update evidence graph review handoff` after all validation evidence is known.
