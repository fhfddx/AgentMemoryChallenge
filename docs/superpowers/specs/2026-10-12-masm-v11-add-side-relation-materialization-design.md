# MASM v1.1 Add-Side Relation Materialization Design

**Date:** 2026-10-12

**Status:** Approved for implementation by the user.

## Context

The last accepted official Smoke baseline scored `30.77`: direct recall and atomic retrieval were
`100`, abstention was `11.11`, and multi-session reasoning was `0`. The later anchor-grounding
family reduced direct recall to `0.00` and total score to `15.38`, so it was fully reverted in
`86f4e30`. That paid result permanently rules out any Search rule that requires returned evidence
to repeat a question identifier or that treats an absent relation graph as permission to retain
only an anchor memory.

The bounded two-hop Search change also left paid multi-session reasoning at `0.00`. Its local probe
showed that the synthetic chain usually had no persisted relation neighbours. The current Add
pipeline already runs perception, history recall, temporal analysis, and curation, but only Curator
actions reach `ValidatedActions`. `TemporalRelationResult.relations` is not persisted, and Curator
can only link memories that the single raw-text history query happened to recall. The most likely
remaining bottleneck is therefore relation formation during Add, not deeper Search traversal.

The target for this line is one candidate capable of raising the official Smoke score to at least
`40` while preserving the baseline's direct-recall and atomic-retrieval strengths. The hidden
evaluation cannot be reproduced locally, so `40` is a target rather than a guarantee. Exactly one
candidate will be handed to the user for one manually initiated paid Smoke after all local and free
gates pass.

## Goals

1. Materialize high-precision, same-user relation edges between memories added in different
   sessions when their original evidence establishes a real factual continuation, shared event, or
   explicit reference.
2. Improve relation-candidate coverage for paraphrases without making literal identifier overlap a
   requirement.
3. Add at most one LLM call to an Add that has historical candidates; add no Search model call.
4. Preserve source evidence, public Add/Search schemas, user isolation, response limits, and all
   existing Search fallback behavior.
5. Fail closed for relation enrichment: invalid or unavailable linker output creates no new links
   but does not discard the otherwise valid Add result.
6. Make the connected and disconnected sides of the multi-session hypothesis independently
   observable through aggregate, content-safe probe results.

## Non-goals

- Do not add or change question-anchor validation.
- Do not require any identifier from a Search question to appear in evidence.
- Do not infer connectivity when persisted relation edges are absent.
- Do not change the selector prompt, selector schema, selector fallback, RelationExpander, reranker,
  relevance gate, response packer, or public API schemas.
- Do not add another Search round or a Search-time model call.
- Do not perform destructive governance actions through the new linker. Merge, supersede, conflict,
  and duplicate handling remain owned by the existing Curator path.
- Do not add a database migration or backfill existing memories. Official evaluation creates fresh
  memories, and the existing relation table already stores the required edge and provenance.
- Do not tune gates after seeing a paid result, inspect hidden evaluation inputs, or run more than
  one paid Smoke for this candidate.

## Architecture

### 1. Bounded history candidate builder

Replace `AddPipeline._recall(request)` with a focused component that receives the current request
and its `PerceptionResult`.

It builds at most four text-query variants in stable order:

1. the complete original Add text;
2. a bounded phrase containing observed entity names;
3. a bounded phrase containing perception keywords;
4. a de-duplicated combination of the entity and keyword phrases when it differs from the earlier
   variants.

The variants are passed in one `ParsedQuery` to the existing `BaselineRetriever`; embedding inputs
remain batchable. Metadata entities are also supplied so stored keywords can participate in recall.
All retrieval remains SQL-scoped to `user_id` and context-granularity memories.

The final history list remains under the existing `MAX_HISTORY = 8` hard cap. Six positions are
reserved for hybrid semantic candidates. Two positions are reserved for the user's most recent
context memories returned by the existing metadata repository query without a keyword filter.
Semantic candidates keep their fused ordering; recent candidates are appended only when their IDs
are not already present. Unused recent slots return to semantic candidates. This recency tail gives
short or rephrased follow-up messages a bounded chance to see the preceding session without making
recency itself a relation.

No candidate is automatically linked. Candidate building only determines what the relation model
may inspect.

### 2. Dedicated relation linker

Add a `RelationLinkerAgent` with its own versioned prompt and strict structured output. It is called
once only when the bounded history list is non-empty and the current Add has non-empty original
text. First-time users, `max_history=0`, and empty-text/image-only Adds make no linker call.

The model receives:

- the current memory's immutable original text;
- up to eight candidate context memories, each with its exact supplied `memory_id` and content.

Each proposed link contains:

- `target_memory_id`;
- an exact quote from the current Add (`current_evidence`);
- an exact quote from the target candidate (`target_evidence`);
- a confidence in `[0, 1]`.

The prompt defines a link narrowly: the two memories must establish a factual continuation, refer
to the same concrete event or subject in a way useful for later joint reasoning, or explicitly
refer to one another. Mere topic, vocabulary, format, recency, or shared generic terms are not a
link. The model may return no links and may never invent IDs or rewrite evidence.

The output is capped at four proposals. This accommodates a message that joins multiple earlier
facts while preventing dense graph creation. The additional call uses the existing configured LLM,
timeout, retry policy, and observability boundaries.

### 3. Deterministic link validation

Add a validator separate from destructive governance validation. A proposal is admitted only when:

- its target is one of the exact context candidates supplied for the same user;
- its target is not duplicated in the same linker decision;
- its confidence is at least the pre-registered `0.85` threshold;
- `current_evidence`, after whitespace normalization only, is a non-empty substring of the current
  original text under the same normalization;
- `target_evidence`, under the same normalization, is a non-empty substring of the supplied target
  content;
- the four-link hard cap is not exceeded.

This quote validation establishes provenance for an Add-time relation judgment. It never compares
Search questions with evidence and therefore cannot recreate the rejected anchor-grounding rule.

Every admitted proposal becomes a non-destructive `RelationDraft(relation_type="link")` with both
quotes in `evidence_ref`. When the existing Curator already emitted a relation to the same target,
the Curator's more specific action wins and the generic linker edge is omitted. Other validated
Curator governance fields remain unchanged.

Invalid individual proposals are ignored. Schema-invalid output, transport failure, timeout, or a
batch-level invariant violation yields no linker relations but preserves perception, temporal data,
Curator actions, the memory itself, and its immutable source messages. Unexpected programming
errors still follow the pipeline's existing degraded-path handling.

### 4. Pipeline and runtime wiring

The official Add pipeline becomes:

1. parse content;
2. perceive content;
3. build bounded multi-view history candidates;
4. perform existing temporal analysis;
5. perform existing Curator governance;
6. request and validate optional non-destructive relation links;
7. combine Curator actions and new links deterministically;
8. persist the unchanged memory bundle.

Add an explicit `RELATION_LINKED` pipeline state between `CURATED` and `VALIDATED`, including the
no-history/no-output path, so call traces remain finite and testable. `build_runtime` wires the new
agent only for `OFFICIAL_MASM`. Local fake behavior remains deterministic and does not acquire a
model dependency. Runtime audit metadata records the new prompt version but never records content,
IDs, or evidence.

No Search production code changes. Existing relation expansion consumes newly persisted edges.
When no edge is produced, Search sees exactly the reverted `86f4e30` behavior.

## Safety and invariants

- All candidate queries and relation targets are scoped by `user_id` in the repository and again
  checked in the validator.
- Relation candidates are context memories only; message memories cannot become governance targets.
- At most eight candidates enter the model and at most four links leave it.
- The linker cannot delete, merge, supersede, conflict, rewrite, or suppress any memory.
- Source messages and memory evidence remain immutable.
- Linker failure cannot make Add fail solely because optional enrichment was unavailable.
- No raw query, evidence, identifier, database row, or relation quote is added to logs.
- Existing relation expansion, selector limits, response size caps, and cleanup semantics remain
  unchanged.
- Direct recall never depends on relation creation; the paid rephrased-identifier regressions stay
  mandatory.

## Test-first implementation

Production work begins with failing tests for the following behavior:

1. Candidate building uses raw text plus bounded entity/keyword variants, reserves recent-history
   coverage, de-duplicates IDs, preserves stable ordering, and never exceeds eight candidates.
2. Candidate building and validation cannot cross user boundaries and never admit message-granular
   targets.
3. The linker is skipped for empty history, `max_history=0`, and image-only content, and is called
   exactly once for an eligible historical Add.
4. Valid paraphrased cross-session evidence can create a link even when the two records do not
   repeat a common identifier verbatim.
5. Same-topic but disconnected evidence, generic vocabulary overlap, and an isolated recent memory
   create no link.
6. Fabricated IDs, duplicated targets, low confidence, fabricated quotes, and over-cap output create
   no unsafe edges.
7. Existing Curator relations take precedence over a generic link to the same target.
8. Linker schema/provider failures preserve the otherwise valid Add result and create no new edge.
9. A three-Add connected chain persists the two required adjacency relations, while a fourth
   same-topic isolated Add has no relation to the chain.
10. Existing user-isolation, degradation, source immutability, relation-expansion, direct-recall,
    atomic, selector, and paid-regression tests remain green.

The content-safe multi-session probe gains a positive relation-coverage row for the main chain in
addition to its existing isolated-memory row. It reports only aggregate counts and pass/fail flags:
the root/bridge/leaf chain must contain the required persisted adjacency, and the isolated memory
must have zero neighbours from that chain. Connected Search must return every required fact;
disconnected Search must return no forbidden chain fact and may abstain.

## Verification and candidate gates

The candidate is eligible for handoff only when all of the following are true:

1. focused RED/GREEN tests for candidate building, linking, validation, failure degradation, and
   persistence pass;
2. the complete repository suite passes against the isolated local `masm_test` database;
3. `ruff check .`, the configured mypy targets, and `git diff --check` pass;
4. the two paid direct-recall regression shapes added in `86f4e30` remain green;
5. the local content-safe probe proves both connected relation creation and disconnected relation
   isolation for three consecutive fresh run tags;
6. no public schema, database migration, Search model-call count, response cap, or cleanup contract
   changes;
7. a final code review finds no Critical or Important issue.

Thresholds and caps in this document are frozen before any deployed or paid result. A failed local
gate rejects the candidate; it does not justify weakening a gate. The user alone initiates the one
paid official Smoke after reviewing the candidate handoff.

## Paid decision rule

The one paid Smoke accepts this line only if:

- total score is at least `40.00`;
- direct recall remains above `0.00` and does not materially regress from the `30.77` baseline;
- atomic retrieval does not materially regress;
- multi-session reasoning improves above `0.00`;
- service health and cleanup remain valid.

If multi-session remains `0.00`, the Add-side relation-materialization hypothesis is rejected. If
direct recall or atomic retrieval collapses, the candidate is rejected even if the total score
improves. No second paid patch round is permitted for this candidate.

## Deployment boundary

This implementation phase is local only. It may commit and push a candidate branch, but it must not
build or deploy a cloud image, restart any cloud container, alter PostgreSQL, change public routing,
run an official Smoke, or run a Full evaluation without a new explicit user instruction. Any later
deployment remains API-only and must preserve the existing database container ID.
