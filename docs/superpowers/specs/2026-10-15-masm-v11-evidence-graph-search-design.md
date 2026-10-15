# MASM v1.1 Grounded Evidence Graph Search Design

**Date:** 2026-10-15

**Status:** Draft for user review; implementation is not yet authorized

**Branch:** `codex/masm-v11-evidence-graph`

**Design base:** `6ad49ad3e99b725821e5e9d1b053d2a0148fe319`

## 1. Decision

Replace the single holistic evidence-selection decision with an opt-in, query-time grounded
evidence graph. The new path keeps broad recall, restores bounded source conversation context,
asks one structured model call to propose question requirements and grounded support spans, then
uses deterministic validation and graph assembly to decide which original memories to return.

The existing `evidence-selector-v7` remains intact as the default path and as an availability
fallback when the graph planner cannot be called or its output is structurally invalid. The graph
path is disabled by default, requires no database migration, and cannot be deployed or evaluated
officially until its public synthetic and local Add-to-Search gates pass.

This is an architectural change, not another selector-prompt experiment.

## 2. Why the current architecture is exhausted

The same broad architecture has now produced all of the following:

- an observed Smoke score of `38.46` with retrieval `100`, multi-session reasoning `0`, and
  abstention `22.22`;
- an exact `d74ad1c` Smoke score of `23.08`, with direct recall `0`, atomic retrieval `100`,
  multi-session reasoning `0`, and abstention `11.11`;
- local selector probes that can move between partial success and perfect success without a
  production code change;
- end-to-end synthetic Search probes that retrieve direct and cross-session evidence reliably,
  while the official multi-session score remains `0`;
- source-context experiments that improve an established 10-case suite from `5/10` to `9/10`,
  but still treat one incomplete cross-source fragment as sufficient;
- completeness auditing, provenance prompting, partial-abstention rules, query decomposition,
  semantic expansion, and bounded two-hop relation traversal that did not produce a stable
  official improvement.

The common bottleneck is `EvidenceSelector`: one model output simultaneously decides whether the
question is answerable, which candidates are relevant, whether all required links are present,
and the exact minimal index set. An omitted index destroys recall; an over-confident local fragment
creates a false positive; a malformed result activates a broad strong-anchor fallback. Prompt
changes exchange one failure mode for another because the responsibilities remain coupled.

The next design therefore changes the unit of reasoning. The model proposes small, locally
grounded facts and links. Deterministic code validates provenance and assembles the evidence set.

## 3. Goals

1. Recover direct, atomic, cross-source, and bounded multi-session evidence without depending on
   one model-generated minimal index set.
2. Preserve abstention by requiring a complete, validated evidence component before reporting
   `sufficient`.
3. Preserve partial direct evidence when at least one requested requirement is grounded but the
   complete path is absent.
4. Make every accepted support item traceable to an exact quote in an original candidate and every
   accepted graph edge traceable to an allowed, testable edge source.
5. Keep public Add/Search schemas, response limits, user isolation, immutable original evidence,
   deletion semantics, and the existing PostgreSQL schema unchanged.
6. Keep the feature completely opt-in and make rollback an API-only image/config change.
7. Use one graph-planner model call per Search in the normal graph path. A second call is allowed
   only when planner transport/schema validation fails and the request falls back to the frozen
   v7 selector.
8. Establish stability gates before any official Smoke: no result may be accepted because one of
   several repeated runs happened to pass.

## 4. Non-goals

- Do not generate or return a final answer from Search.
- Do not infer the correct multiple-choice option.
- Do not inspect or reproduce hidden evaluation questions, answers, memories, or raw logs.
- Do not tune against individual hidden rows or map diagnostics to hidden test identities.
- Do not change Add-time perception, temporal extraction, curator actions, stored relation
  semantics, or database tables in this iteration.
- Do not create graph edges from embedding similarity, rank proximity, answer options, shared
  generic topics, or an ungrounded model assertion alone.
- Do not remove `EvidenceSelector`, change `evidence-selector-v7`, or weaken its existing tests.
- Do not deploy, push, start Full evaluation, or start an official Smoke as part of implementation.

## 5. Considered approaches

### 5.1 Return a broad Top-K and remove selection

This is the most aggressive high-recall option. It would likely recover some direct and
multi-session evidence, but it makes unrelated candidates visible to the downstream answer stage
and is expected to collapse abstention. It provides no principled way to distinguish a complete
chain from a disconnected lookalike. It is rejected.

### 5.2 Run the existing selector several times and vote

Self-consistency can reduce isolated model variance, but it multiplies latency and provider cost,
retains the same coupled decision contract, and still needs a rule for conflicting index sets. It
is useful as a diagnostic, not as the target architecture. It is rejected.

### 5.3 Persist a full claim graph at Add time

An Add-time fact store could support powerful graph queries, but it requires new persistence,
migration, deletion, idempotency, backfill, and degradation semantics. Previous Add-time relation
work also showed that missing write-time edges cannot be repaired at Search. This is too risky for
the next candidate and is deferred.

### 5.4 Build a grounded evidence graph at Search time

This reuses the existing recall pool and immutable source evidence, adds no migration, and moves
global selection into deterministic code. It can use persisted relation provenance, same-source
adjacency, and explicit cross-source mentions while refusing invented bridges. This is the
selected approach.

## 6. High-level pipeline

With `MASM_EVIDENCE_GRAPH_ENABLED=0`, Search behaves exactly as it does at the design base.

With graph mode enabled, the official MASM Search path is:

1. analyze the original query with the existing `QueryAnalyzer`;
2. run the existing bounded hybrid recall;
3. apply the existing question-derived `RelevanceGate` and record strong anchors;
4. run persisted relation expansion and retain an internal provenance sidecar for emitted edges;
5. deduplicate and rerank exactly as today;
6. enrich eligible message candidates with bounded same-request source context;
7. build the existing deterministic, source-diversified selector pool, capped at 32 candidates;
8. call `EvidenceGraphPlanner` once with the original question and bounded candidates;
9. validate question quotes, candidate quotes, candidate indices, source ownership, graph edges,
   limits, and internal consistency without another model call;
10. assemble complete or partial evidence from the validated graph in original rank order;
11. render original evidence/source context and apply the existing response count/byte limits;
12. emit content-free diagnostics.

Pure-image queries remain on the existing selector/fallback path because the planner receives no
raw query image. Mixed text/image queries may use graph mode only for text-grounded requirements;
the existing original-media renderer remains responsible for the returned image evidence.

## 7. New components and boundaries

### 7.1 `EvidenceGraphPlanner`

Create `src/masm/retrieval/evidence_graph.py`. The planner owns the structured provider request and
nothing else. It receives:

- original question text, truncated by the existing question limit;
- `visual_query_present`;
- options as explicitly untrusted context;
- at most 32 candidates with opaque index, source-group label, source position, granularity,
  bounded text/source context, and existing retrieval signals.

It does not receive raw user IDs, request IDs, memory UUIDs, object URIs, image bytes, database
metadata, or credentials.

The planner must not answer the question. It proposes requirements, grounded support spans, and
candidate-to-candidate links. It does not return `selected_indices` or an `evidence_state`.

### 7.2 Structured proposal

Use strict frozen Pydantic models with `extra="forbid"` and strict integer indices.

`QuestionRequirement`:

- `question_quote`: non-empty exact text copied from the question;
- `kind`: one of `subject`, `attribute`, `relation`, `time`, `location`, `count`, `comparison`.

`GroundedSupport`:

- `requirement_index`: index into the requirement list;
- `candidate_index`: index into the bounded candidate pool;
- `candidate_quote`: non-empty exact text copied from that candidate;
- `role`: one of `direct`, `bridge`, `context`, `negative`.

`ProposedLink`:

- `left_candidate_index` and `right_candidate_index`;
- `left_quote` and `right_quote`, copied exactly from their candidates;
- `kind`: one of `same_source_context`, `persisted_relation`,
  `shared_explicit_mention`, `confirmation`, `correction`.

`EvidenceGraphProposal`:

- `requirements`: 1 to 4 items;
- `supports`: at most 24 items;
- `links`: at most 24 items.

The model may omit support rather than guess. No free-form rationale, canonical entity name,
answer value, confidence score, summary, or final state is allowed.

### 7.3 Provenance sidecar

The graph validator needs to distinguish an actual stored relation from a model-invented link.
Extend `RelationExpander` with an additive internal method such as
`expand_with_provenance(...) -> ExpansionResult`. `ExpansionResult` contains the current expanded
candidates plus bounded `ExpansionEdge` records with predecessor memory ID, candidate memory ID,
relation type, and depth. Keep the existing `expand(...) -> list[MemoryCandidate]` behavior and
tests unchanged by delegating to the new method and returning only candidates.

Search converts context-memory edge endpoints to source-group pairs before graph planning. The
planner sees opaque source labels; only the deterministic validator sees the sidecar. The sidecar
is never logged or returned publicly.

### 7.4 `GroundedGraphValidator`

Validation is fail-closed and deterministic:

1. Requirements must be non-empty, within the cap, and have a normalized exact-substring match in
   the original question.
2. Requirement indices and candidate indices must be unique where required and in range.
3. Every candidate quote and edge quote must be a normalized exact substring of the exact text
   shown to the planner. Normalization may apply Unicode NFKC, case folding, and whitespace
   collapse; it must not paraphrase or delete negation.
4. Every support candidate must belong to the request user and the bounded pool.
5. `same_source_context`, `confirmation`, and `correction` links require the same non-empty
   source group and adjacent source positions represented in the enriched context.
6. `persisted_relation` requires a matching edge in the expansion provenance sidecar. A model
   proposal alone is never sufficient.
7. `shared_explicit_mention` requires the two grounded quotes to share an explicit discriminative
   unit that is absent from the question. For Latin/alphanumeric text, accept either a shared
   multi-token phrase or a shared token of at least six characters. For CJK text, accept a shared
   contiguous run of at least two characters. Reject units consisting only of punctuation,
   numbers shorter than four digits, stopwords, or generic relation nouns configured in a small,
   reviewed multilingual denylist. Add no NLP dependency.
8. An invalid item is dropped. If structural limits, requirement definitions, or indices are
   invalid, the entire proposal is invalid and invokes v7 fallback.
9. A proposal may not convert an option string into support unless the same fact is quoted from a
   candidate.

The validator returns a content-free failure category from a fixed enum. It never logs quotes or
identifiers.

### 7.5 `EvidenceGraphAssembler`

The assembler derives the state; it does not trust a model-supplied global verdict.

- A requirement is covered when it has at least one validated `direct` or `negative` support.
  `bridge` and `context` nodes can connect evidence but cannot cover a requirement alone.
- A one-node direct fact is a valid connected component.
- For multiple requirements, all covering nodes must belong to one connected component through
  validated links. Separate candidates that merely share a rank, option, or topic do not form a
  component.
- `sufficient`: every requirement is covered in one valid component and the complete evidence set
  fits within the existing 12-item cap.
- `partial`: at least one requirement has validated direct/negative support, but complete coverage
  or connectivity is absent. Return only the best connected component containing direct support.
- `insufficient`: no requirement has validated direct/negative support. Return no evidence.

For a complete graph, return the union of requirement-covering nodes and the shortest validated
paths connecting them. Prefer fewer nodes, then more direct supports, then more distinct sources,
then original rank. Preserve original rank in the public result. Never add a candidate solely to
fill the response budget.

Conflicting positive/negative support must preserve both sides when they form a stored conflict
group. Otherwise the proposal is partial rather than silently choosing one side.

### 7.6 Compatibility adapter

Keep the existing `EvidenceSelector.select(...)` signature unchanged. Add a separate
`GraphEvidenceSelector.select(query, options, ranked, strong_anchor_ids, graph_context) ->
SelectionResult` entry point. `graph_context` is an internal immutable object containing only the
bounded source-group relation pairs and same-source adjacency available for this Search request.
It is never part of a public schema.

`SearchService` keeps the existing v7 selector dependency and gains one optional graph-selector
dependency. When graph mode is off, the graph dependency is `None` and the existing call path is
byte-for-byte unchanged. When graph mode is on, Search builds `graph_context` from the expansion
sidecar and source metadata, then calls the graph selector. `GraphEvidenceSelector` holds the v7
selector as its failure fallback; no request-scoped state is stored on either selector instance.

When graph mode is enabled:

- valid graph proposals use graph assembly;
- valid `insufficient` results abstain and do not fall back;
- provider transport failures, schema failures, and structurally invalid proposals invoke the
  unchanged v7 selector once with the same pool;
- pure-image queries invoke the existing path directly;
- if both planner and v7 fallback fail, retain the existing v7 failure/fallback semantics rather
  than inventing new evidence.

## 8. Configuration

Add only bounded, validated settings:

- `MASM_EVIDENCE_GRAPH_ENABLED`, default `0`;
- `MASM_EVIDENCE_GRAPH_MAX_REQUIREMENTS`, default and hard cap `4`;
- `MASM_EVIDENCE_GRAPH_MAX_SUPPORTS`, default and hard cap `24`;
- `MASM_EVIDENCE_GRAPH_MAX_LINKS`, default and hard cap `24`;
- `MASM_EVIDENCE_GRAPH_TIMEOUT_SECONDS`, positive and no greater than the global model timeout.

Graph mode requires the existing selector and source-context features to be enabled. Runtime
validation rejects inconsistent combinations rather than silently enabling dependencies. The
official model remains `gpt-4o-mini`; no additional provider or model is introduced.

## 9. Failure handling

| Failure | Required behavior |
| --- | --- |
| Empty recall pool | Return empty without a provider call |
| Planner transport/timeout failure | Run frozen v7 selector once; record `planner_unavailable` |
| Planner schema/structural failure | Run frozen v7 selector once; record `planner_invalid` |
| Some invalid support/link items | Drop invalid items, assemble from remaining valid graph |
| Valid graph with no direct support | Abstain; do not fall back |
| Complete graph exceeds 12 evidence items | Return best bounded partial component, never truncate a claimed complete path |
| Source-context lookup unavailable | Use original candidate text; same-source contextual edges become unavailable |
| Pure-image query | Use existing selector/fallback path |
| Unexpected internal invariant failure | Fail closed to frozen v7 path; do not return HTTP 500 if v7 succeeds |

No failure path may broaden to all recalled candidates.

## 10. Privacy, isolation, and observability

All retrieval, source lookup, relation lookup, graph validation, rendering, and cleanup remain
scoped by `user_id`. Cross-user candidates or edges are rejected even if supplied by a faulty
repository fake or corrupt row.

Diagnostics may include only:

- graph mode enabled;
- candidate, requirement, support, validated-link, component, selected, and selected-source counts;
- derived state;
- whether v7 fallback ran;
- fixed failure category;
- planner and total Search latency.

Diagnostics must not include questions, options, quotes, candidate text, model output, memory IDs,
request IDs, user IDs, relation endpoints, object URIs, credentials, or provider bodies.

## 11. Test-first verification

Implementation must be test-driven. Before product code, add a focused test module that fails
because graph components do not yet exist. Required cases:

1. direct one-source fact;
2. two requested attributes from two sources;
3. explicit cross-source `Nora -> Zephyr workshop -> Lisbon` chain;
4. same-source elliptical confirmation using adjacent role-preserving context;
5. forward correction where the corrected value wins and both quotes remain available;
6. bounded persisted two-hop relation with provenance sidecar;
7. wrong-subject complete-looking chain that abstains;
8. disconnected bridge with a shared generic noun that abstains;
9. missing requested value that abstains;
10. multiple-choice option overlap that cannot create support;
11. high-ranked and strong-anchor distractors that remain excluded;
12. crowded 32-candidate positive and negative cases;
13. conflict group preservation;
14. duplicate context/message representations from one source counting once;
15. Chinese direct, contextual confirmation, and disconnected cases;
16. invalid quotes, duplicate/out-of-range indices, excessive requirements/supports/links;
17. planner unavailable/invalid falling back to unchanged v7 behavior;
18. valid insufficient graph never falling back;
19. pure-image behavior unchanged;
20. logs and diagnostics containing no content or raw identifiers.

Add property-style tests for deterministic ordering, caps, cycles, diamonds, and permutation of
unrelated candidates. Add an integration test with the real repository/Search path proving that a
relation provenance sidecar cannot cross users and that rendering returns exactly the text/context
seen by the planner.

## 12. Predeclared candidate gates

Graph mode is not eligible for an API candidate unless all gates pass:

1. New focused tests demonstrate RED on the frozen base before implementation and GREEN afterward.
2. Existing evidence-selector, relevance, reranker, source-context, relation-expansion, rendering,
   deletion, isolation, API-contract, and response-budget tests pass unchanged.
3. Complete `pytest -q`, Ruff, mypy, and `git diff --check` pass.
4. A fixed public synthetic suite contains at least 20 paired positive/negative cases and passes
   deterministically with fake providers.
5. A single predeclared real-provider stability run executes the whole fixed suite five times;
   eligibility requires every case to pass in every round, with zero provider/schema failures and
   no cherry-picking. One failure rejects the candidate; do not tune and rerun the same gate.
6. Local real Add-to-Search probes cover direct recall, atomic retrieval, contextual confirmation,
   connected cross-source reasoning, disconnected abstention, wrong subject, crowded candidates,
   and exact cleanup with zero residual rows/assets.
7. Concurrency 16/16 passes without exceeding current response, timeout, and memory limits.
8. The feature is still default-off and the production `src` diff contains no unrelated Add,
   deletion, storage-schema, v1.0, or public API change.

Passing these gates proves only that the candidate is coherent enough for a controlled API-only
canary. It does not prove an official score.

## 13. Deployment and rollback boundary

DeepSeek implementation ends after local commits and verification. It must not push, deploy, edit
server configuration, read production logs, or start an evaluation.

A later authorized deployment must:

1. build an immutable image from an exact reviewed commit;
2. record API and PostgreSQL container IDs and Compose config paths;
3. add a dedicated override enabling graph and source-context flags;
4. replace only `masm-v11-api-1` with `--no-deps --force-recreate api`;
5. prove `masm-v11-postgres-1` has the same container ID;
6. require healthy API, zero restarts, exact prompt/mode metadata, and all non-official probes;
7. roll back immediately to the recorded image/config on any failure;
8. run at most one user-authorized Smoke after all gates; never run Full.

## 14. Explicitly forbidden shortcuts

- Do not modify only the prompt version and call it an evidence graph.
- Do not treat `strong_anchor_ids` as proof of relevance or connectivity.
- Do not construct edges from shared options, embedding similarity, rank proximity, or generic
  words such as “project”, “workshop”, “meeting”, or “city”.
- Do not let the planner output a final answer or free-form rationale.
- Do not accept a quote that is not present in the exact planner-visible candidate text.
- Do not suppress failing tests, loosen expected outcomes, or repeat provider runs until one passes.
- Do not overwrite or remove the existing v7 selector or source-context experiments.
- Do not access or commit local secret files, `.env` contents, raw official logs, database dumps,
  or evaluation artifacts.

## 15. Acceptance of this specification

Approval of this document authorizes implementation planning, not product-code changes by Codex.
The user intends DeepSeek to implement the reviewed design. DeepSeek must first present a concrete
task plan and wait for the user's approval in that session before editing code. Codex will later
review the actual diff and verification evidence in a new conversation.
