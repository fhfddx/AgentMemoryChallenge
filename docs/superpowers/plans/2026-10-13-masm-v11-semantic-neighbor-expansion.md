# Semantic Neighbor Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover cross-session bridge evidence without weakening direct retrieval or abstention.

**Architecture:** Add a user-scoped repository primitive for stored seed vectors and excluded-request vector search, a bounded `SemanticNeighborExpander`, and an optional SearchService/runtime integration after the relevance gate. Preserve direct candidates as the sole selector anchors.

**Tech Stack:** Python 3.12, SQLAlchemy, PostgreSQL/pgvector, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-13-masm-v11-semantic-neighbor-expansion-design.md`

## Global constraints

- Never read, display, move, delete, stage, or commit `秘钥.txt`.
- Never reintroduce question-identifier grounding, connectivity pruning without
  real relation edges, or bounded two-hop assumptions.
- Never change evaluation gates after observing results.
- Never touch v1.0 or PostgreSQL lifecycle; deployment is API-only.
- Never run Full evaluation. Run a paid Smoke only after every declared gate.

### Task 1: Repository primitives

**Files:**
- Modify: `src/masm/storage/repositories.py`
- Modify: `tests/unit/storage/test_repository_scope.py`

- [ ] Add failing integration tests for stored text-vector reads scoped by user,
  model, version, and requested memory IDs.
- [ ] Add failing tests for vector retrieval excluding specified request IDs.
- [ ] Implement the smallest repository changes that make those tests pass.
- [ ] Run the focused repository tests and commit the task.

### Task 2: Semantic neighbor expander

**Files:**
- Create: `src/masm/retrieval/semantic_expander.py`
- Create: `tests/unit/retrieval/test_semantic_expander.py`
- Modify: `src/masm/retrieval/__init__.py`

- [ ] Add failing tests for distinct-request seeds, bounds, similarity floor,
  stable deduplication, user isolation, and missing-vector no-op behavior.
- [ ] Implement the expander with fixed caps and `text_vector` retrieval signals.
- [ ] Run focused expander tests and commit the task.

### Task 3: Search and runtime wiring

**Files:**
- Modify: `src/masm/services/search_service.py`
- Modify: `src/masm/runtime.py`
- Modify: `tests/unit/services/test_search_service.py`
- Modify: `tests/unit/test_runtime_factory.py`

- [ ] Add failing tests proving expansion happens after relevance gating, expanded
  evidence reaches selection, and only direct candidates remain strong anchors.
- [ ] Wire the optional expander into SearchService and OFFICIAL_MASM runtime only.
- [ ] Keep LOCAL_FAKE behavior unchanged.
- [ ] Run focused service/runtime tests and commit the task.

### Task 4: Discriminating probe and final gates

**Files:**
- Create: `scripts/semantic_expansion_probe.py`
- Create: `tests/unit/test_semantic_expansion_probe_script.py`

- [ ] Add a failing probe test for safe aggregate-only output and cleanup.
- [ ] Implement a bounded connected/disconnected synthetic probe with no raw
  content, IDs, logs, database rows, or credentials in output.
- [ ] Run the probe locally three times and confirm it discriminates the candidate
  from the `d74ad1c` behavior.
- [ ] Run full `pytest`, `ruff check .`, required `mypy`, and `git diff --check`.
- [ ] Perform a final safety-focused diff review, commit, and push through the local
  proxy. Stop before deployment unless all gates pass.
