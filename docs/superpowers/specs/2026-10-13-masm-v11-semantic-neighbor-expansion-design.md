# MASM v1.1 Semantic Neighbor Expansion Design

## Problem

Paid Smoke results show that `d74ad1c` preserves direct and atomic retrieval but
still scores `0.00` on multi-session reasoning. Query decomposition did not change
that outcome. Anchor grounding and connectivity pruning are rejected because they
collapsed valid direct recall or discarded disconnected-but-required evidence.

## Decision

Add a bounded, additive semantic-neighbor expansion stage after the existing
relevance gate. Strong direct hits become seeds. Their stored text embeddings are
used to retrieve semantically similar memories from other Add requests belonging
to the same user. The original candidate set is never filtered or reordered by the
expander itself.

The existing reranker and `evidence-selector-v7` remain the only mechanisms that
decide which candidates are returned. Expanded candidates are not strong anchors,
so selector failure cannot promote them into fallback evidence.

## Safety invariants

- Every embedding read and neighbor query is scoped by `user_id`.
- Seed embeddings must match the active text embedding model name, version, and
  vector dimension.
- Neighbors must come from a different `request_id` than every seed request.
- Existing candidates and duplicate memory IDs are not added again.
- Similarity must meet the existing configured text-similarity floor.
- Seed count, neighbors per seed, and total expansion are hard bounded.
- Empty or missing seed embeddings degrade to no expansion.
- Expansion never requires a question identifier, relation edge, graph
  connectivity, or a bounded-hop path.
- Query analysis, Add, relation expansion, evidence selection prompts, and public
  API schemas are unchanged.

## Pipeline

1. Recall and fuse the normal query candidates.
2. Apply the existing relevance gate.
3. Capture strong-anchor IDs from the direct gated candidates.
4. Select up to four distinct-request seeds in current candidate order.
5. Load those seeds' stored text embeddings for the active model/version.
6. Retrieve up to eight cross-request neighbors per seed, filter by the configured
   text-similarity floor, deduplicate stably, and cap the expansion at 24 items.
7. Continue with existing relation expansion, deduplication, reranking, selector,
   renderer, and response packing.

## Validation gates

The candidate may be deployed only when all of the following pass:

- Unit tests prove bounds, stable deduplication, cross-request exclusion, model
  scoping, user isolation, and missing-vector no-op behavior.
- Search-service tests prove expanded evidence reaches the selector while only
  direct candidates remain strong anchors.
- PostgreSQL integration tests prove seed-vector reads and excluded-request vector
  retrieval remain user/model scoped.
- A content-safe semantic-expansion probe demonstrates connected cross-session
  recovery and abstention on a disconnected lookalike in three consecutive runs.
- Full `pytest`, `ruff check .`, required `mypy`, and `git diff --check` pass.
- The deployed API is healthy and the PostgreSQL container ID is unchanged.

Only after those gates pass may one paid Smoke be run. Failure of the targeted
probe rejects the hypothesis without spending a Smoke.
