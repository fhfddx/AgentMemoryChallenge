# MASM v1.1 Bounded Two-Hop Multi-Session Retrieval Design

**Date:** 2026-10-10

**Status:** Approved by the user for implementation planning.

## Context

The deployed `84926f6` candidate uses `evidence-selector-v7`. Its official Smoke result is
`30.77`, with direct recall and atomic retrieval at `100`, abstention at `11.11`, and
multi-session reasoning at `0`.

A deployed, content-safe `search_path_probe.py` run against this image passed every boundary:

- both synthetic sessions were stored with all four registered markers;
- direct recall returned its one required marker;
- a two-session question returned both required facts;
- a same-entity question with no supporting fact correctly returned no evidence;
- three cleanup passes left zero residual memories and sources.

This proves that basic retrieval from two independent Add requests works end to end. It does not
prove that the system can traverse a longer reasoning chain. The current implementation and its
tests explicitly restrict `RelationExpander` to one hop: an admitted anchor `A` may add `B`, but a
relation from `B` to `C` is never followed. That is the narrowest concrete gap consistent with the
passing direct two-session probe and the still-zero public multi-session score.

The public aggregate does not reveal any hidden question or establish that two-hop expansion is
the official failure's cause. This change is therefore an independently testable hypothesis, not
a claim about hidden evaluation content.

## Goals

1. Make a bounded `A -> B -> C` memory chain available to Search when `A` is a strong,
   question-admitted anchor.
2. Preserve the current selector prompt, schema, three-state semantics, fallback, and limits.
3. Prevent graph traversal from admitting disconnected, merely similar memories.
4. Preserve user isolation, deterministic ordering, conflict completion, candidate caps, response
   caps, and the public Add/Search contracts.
5. Add public connected and disconnected multi-session regression cases before changing production
   behavior.

## Non-goals

- Do not change `evidence-selector-v7` or selector pool construction.
- Do not add another model call or generate a final answer in Search.
- Do not change Add-time perception, temporal analysis, curation, or relation creation.
- Do not infer new relations from candidate text, embeddings, shared topics, or answer options.
- Do not add a database migration or alter the relation schema.
- Do not inspect, reproduce, or tune against hidden evaluation inputs.
- Do not run another official Smoke until the local and deployed synthetic gates pass.

## Considered approaches

### Bounded two-hop query-time relation expansion

Follow one additional persisted memory-to-memory edge from first-hop neighbors. This adds one
bounded database relation lookup only when a first hop exists. It uses explicit stored provenance,
does not add a model call, and can be tested deterministically. This is the selected approach.

### Stronger Add-time relation creation

Prompting Add-time agents to create more links could improve graph coverage, but it changes write
semantics and affects all subsequently stored data. It also cannot repair an existing relation
that is present but unreachable because Search stops after one hop. This is deferred.

### Model-driven iterative retrieval

A model could read first-round evidence and generate a follow-up query for an implicit bridge. It
would cover chains without stored edges, but adds latency, data flow, cost, and a new failure mode.
It is not justified before testing the smaller graph-traversal gap.

## Search pipeline

The Search pipeline remains:

1. analyze the original query;
2. run bounded hybrid recall;
3. apply the existing relevance gate;
4. record the admitted memories as strong anchors;
5. expand explicit relations by at most two hops;
6. deduplicate and rerank;
7. run the unchanged evidence selector;
8. rehydrate original evidence and apply response limits.

Only step 5 changes. Second-hop candidates never become strong anchors and therefore do not affect
selector fallback. They are ordinary expanded candidates that must still survive reranking and the
unchanged evidence selector.

## Expansion algorithm

`RelationExpander.expand(user_id, seeds, limit)` keeps its public signature. The implementation
continues to bound initial seeds by `MAX_SEEDS = 8` and total emitted expansion by
`MAX_EXPANDED = 32` and the caller-provided `limit`. A new internal
`MAX_SECOND_HOP = 8` prevents the deeper hop from displacing most direct neighbors.

The algorithm is:

1. Bound and de-duplicate the original seed IDs.
2. Complete conflict groups for the original seeds. Conflict peers retain first priority in the
   output budget, matching current behavior.
3. Read first-hop memory neighbors for the original seeds, bounded by the shared expansion budget.
4. Select at most `MAX_SEEDS` deterministic first-hop neighbors as second-hop traversal seeds.
5. If the first-hop seed list is non-empty, read at most `MAX_SECOND_HOP` of their memory neighbors
   in one additional repository call.
6. After conflict peers consume their priority slots, reserve at most `MAX_SECOND_HOP` remaining
   slots for second-hop results while retaining at least one first-hop slot whenever a first-hop
   result exists. Unused reservation returns to the first hop.
7. Emit conflict peers, the admitted first-hop quota, and the admitted second-hop quota in that
   priority order, de-duplicating against original seeds and every previously emitted ID.
8. Stop exactly at the shared expansion budget.

The repository's `related()` method continues to perform every query with `user_id` filtering in
SQL. The second hop must call that same method; it must not bypass repository isolation.

Both database reads are capped, and no more than eight second-hop candidates can enter the output.
When the caller's budget is too small to contain both hops, the expander preserves conflict peers
and the earliest deterministic first-hop evidence rather than exceeding the cap. With a normal
budget of at least two, an available first-hop bridge and one available second-hop endpoint can
both be retained.

Cycles such as `A -> B -> A`, reciprocal edges, repeated edges, and diamonds such as
`A -> B/C -> D` emit each memory at most once. If there is no first-hop neighbor, the second
repository call is skipped.

## Ordering and source rehydration

Within each relation hop, candidates remain deterministically ordered by descending stored score
and then memory ID. Conflict peers remain ordered by conflict-group ID and memory ID.

Search already converts recalled message seeds to their same-request context parents before
relation expansion because governance edges attach to context memories. After expansion, Search
already loads bounded message evidence for each expanded context's `request_id`. Two-hop contexts
use this existing path without a new response representation.

The final rank is still decided by the existing reranker. Hop distance is not exposed in the
public response and does not receive a ranking bonus.

## Safety and invariants

- Traversal uses only persisted memory-to-memory relations; lexical or semantic similarity cannot
  create a bridge.
- Every relation read remains scoped to the request's `user_id`.
- At most two relation lookups occur for ordinary neighbors.
- Initial and second-hop traversal seeds remain capped at eight, no more than eight second-hop
  candidates enter the output, and total expansion remains capped at 32.
- Original seeds and duplicate paths cannot be returned as expansion results.
- Conflict-group completion retains priority and identity.
- Selector behavior, fallback anchors, returned evidence count, response byte budget, and public
  schemas are unchanged.
- Diagnostics remain aggregate-only; this design does not add query, evidence, identifiers, or
  relation contents to logs.

## Test-first verification

Production code must not change until the new connected test fails against the frozen baseline.

The first red test constructs an in-memory graph `A -> B -> C`, searches from `A`, and requires the
expanded result to contain both `B` and `C`. On the baseline it must fail because only `B` is
returned.

The paired negative test includes an unconnected `D` with similar content and requires it to stay
absent. Additional tests cover:

- reciprocal and cyclic edges without duplicate output;
- a diamond graph with one copy of the shared endpoint;
- no second repository call when the first hop is empty;
- second-hop seed and total-result hard caps;
- conflict peers retaining budget priority;
- SearchService rehydrating message evidence for a second-hop context;
- existing cross-user relation-isolation tests remaining green;
- all selector unit and deployed selector probes remaining unchanged and green.

After focused red/green/refactor verification, run the full repository suite, Ruff, mypy over the
configured production and script targets, and `git diff --check`.

## Deployment and evaluation

The implementation is an independent candidate based on commit `65738a44b181` in branch
`codex/masm-v11-multihop`. It must use a new immutable image tag and an API-only Compose override.
The PostgreSQL container must keep the same ID.

Before any paid evaluation:

1. confirm a clean source tree and record the exact commit and image ID;
2. run the existing selector Gates 1-3 without changing their expected values;
3. run the existing deployed `search_path_probe.py` and confirm exact cleanup;
4. run a new content-safe multi-hop probe using only isolated synthetic Add/Search/Delete requests,
   aggregate counts, and exact cleanup;
5. verify API health, zero restarts, and an unchanged database container ID.

Only one official Smoke may be run for this candidate after all gates pass. Compare the public
multi-session score and guard against regressions in direct recall, atomic retrieval, abstention,
service health, and cleanup. A flat or worse public result rejects the hypothesis; it does not
justify mixing selector changes into this candidate.

## Rollback

Rollback is API-only: restore the recorded `masm-v11-final-candidate:84926f6` override and recreate
only `masm-v11-api-1`. Confirm that the PostgreSQL container ID is unchanged and that the restored
API reports `running`, `healthy`, and zero restarts. No database rollback or migration is required.

## Follow-up (2026-10-11): per-evidence anchor grounding

The third deployed round of `multi_session_path_probe.py` on `015986a` returned
`evidence_state=partial` with `selected_count=2`, `matched_marker_count=1` and
`forbidden_marker_count=2`. The selector had selected the isolated record that carries the query
anchor `Unconnected-<tag>` **plus** an unrelated chain record. `relation_isolation` still reported
`neighbor_count=0`, and the recall, expansion, and selector-pool counts matched the passing
rounds, so the failure is confined to the model selection step.

The set-level check added in `015986a` only proved that the *union* of the selected text mentions
an anchor. The required property is per-evidence: every selected memory must lie in the
anchor-rooted, user-scoped allowed-evidence component.

A first attempt (`c7eede8`) defined that component only over persisted relation edges through
`masm.retrieval.anchor_connectivity.AnchorConnectivity`. The deployed round 1 of the same probe
rejected it: with the model output unchanged (`[root, bridge, leaf]`, `sufficient`), the wired
selector returned only 1 of 3 items and covered 2 of 4 markers, because `related()` found no
neighbours for that chain. Relation-only grounding collapsed to "keep the anchor's own memory",
while the probe's `connected_chain` case passes on `015986a` without any traversal at all, since
every run enters the candidate pool through recall.

The allowed path is therefore defined over **two** edge sources. Two selected memories are linked
when they share an explicit structured identifier (matched per evidence pair, so a downstream link
may share `Bridge-*`/`Leaf-*` instead of repeating `Root-*`), or when they are connected inside the
bounded relation closure that `AnchorConnectivity` returns (message evidence is mapped to its
same-request context parent first, because governance edges attach to context memories). The
selector keeps the component rooted at the anchor-bearing evidence, drops the rest, and downgrades
`sufficient` to `partial` when anything was dropped. `build_runtime` wires the relation oracle for
the official profile; identifier adjacency needs no database and therefore applies even when no
oracle is wired. The prompt, schema, three-state semantics, fallback anchors, pool construction,
relation-expansion budgets, and public Add/Search contracts are unchanged.
