# MASM v1.1 Evidence Graph Review Fixes Design

**Date:** 2026-10-15
**Status:** Approved for implementation by the user's instruction to optimize the reviewed candidate
**Parent design:** `docs/superpowers/specs/2026-10-15-masm-v11-evidence-graph-search-design.md`

## Goal

Close the independently reproduced correctness, isolation, boundedness, and failure-output gaps without changing the public Add/Search schemas, database schema, default-off rollout, or frozen v7 success-path behavior. The result is still only a local API-only canary candidate; this work does not deploy or run an evaluation.

## Decisions

### 1. Request user scope is an explicit selector input

`SearchService` passes the request's `user_id` to `GraphEvidenceSelector`. The selector filters before pool construction and the validator independently rejects any candidate view outside that user. Foreign candidates never enter the planner payload or either fallback path.

### 2. A graph failure gets exactly one frozen-v7 call over the same bounded pool

Planner unavailability, malformed output, failed validation, or internal graph assembly failure may invoke frozen v7 once. That call receives the already user-scoped graph pool, whose size is at most 32, so v7 cannot perform its 32-to-48 deep retry. A valid `insufficient` graph remains an abstention and never falls back.

### 3. All valid support combinations are examined

The caps permit at most four requirements and 24 supports. The largest balanced Cartesian product is `6^4 = 1296`, which is small and deterministic. Assembly therefore enumerates the complete bounded product instead of replacing products above 512 with the first support for each requirement.

### 4. Over-budget partial evidence remains connected

If a complete connected interpretation exceeds the 12-item response cap, assembly searches non-empty requirement subsets and their covering-node combinations. It chooses the deterministic connected path union that covers the most requirements, then prefers direct support, source diversity, fewer nodes, and original rank. It never preserves disconnected endpoints merely because they are covering nodes.

### 5. Stored conflict membership is deterministic internal provenance

For a single requirement, a validated direct support and validated negative support with the same non-null stored `conflict_group_id` receive an internal adjacency edge. This edge is derived only from stored candidate metadata after quote and user validation. It is not a planner link kind, is not accepted from planner output, and is not reported as persisted-relation provenance. A resolved conflict is sufficient only when both sides are included in one connected interpretation.

### 6. Relation provenance is bounded in SQL

`MemoryRepository.relation_edges` returns only relations whose source and target IDs are both in the supplied traversal set and both owned by the requested user. Ordering and `LIMIT` are applied in SQL. `RelationExpander` supplies both endpoints for seed, first-hop, and second-hop frontier queries; legacy `expand()` remains unchanged and does not request provenance.

### 7. SQL exceptions redact bound parameter values

Every engine created through `Database.create` enables SQLAlchemy parameter hiding. This prevents user IDs, request IDs, candidate text, and other bound values from appearing in exception strings or engine logs while leaving SQL shape and application behavior unchanged.

## Compatibility and non-goals

- No public API schema, response rendering, database migration, or relation write semantic changes.
- The evidence-graph flag remains disabled by default; disabled searches make zero graph-planner calls.
- No new planner fields, free-form rationale, answer text, selected-index verdict, global state, or conflict link kind.
- No changes to multimodal routing, local-fake behavior, response budgets, 16-concurrency behavior, deletion, or source rendering.
- No push, deployment, provider call, Smoke/Full evaluation, or online database access.

## Acceptance criteria

1. A foreign candidate ranked first cannot enter the planner or v7 fallback for another request user.
2. A graph planner failure over 40 ranked items produces exactly two provider calls total: graph once, v7 once.
3. A connected solution beyond the former 512th combination is found as `sufficient`.
4. Any over-budget `partial` selection induces one connected component.
5. Opposing supports in the same stored conflict group are both selected and can be `sufficient` without a planner-declared link.
6. Provenance queries are SQL-bounded, cross-user safe, and preserve old `expand()` ordering/caps.
7. SQL exception text does not contain a bound-value canary.
8. Focused tests, full pytest, Ruff, mypy, and `git diff --check` pass locally.
