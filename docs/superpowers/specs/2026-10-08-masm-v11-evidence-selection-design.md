# MASM v1.1 Precision Evidence Selection Design

**Date:** 2026-10-08

**Status:** Approved by the user for implementation planning.

## Context

The latest completed v1.1 Smoke result scored 23.08. Its public aggregate breakdown was:

- retrieval: 66.67;
- direct recall: 0;
- atomic retrieval: 100;
- multi-session reasoning: 0;
- abstention: 11.11.

The implementation already stores message-level evidence, supports bounded relation expansion, and
can retrieve atomic evidence. The remaining failure pattern is therefore primarily a precision and
evidence-selection problem rather than proof that the required information was never stored.

The existing retrieval-relevance design appends every multiple-choice option to the question as an
independent recall variant. That is symmetric, but it also lets distractor options act as strong
anchors. A lexical-channel appearance is currently sufficient for admission even when its score is
zero, and the external `top_k` contract permits up to 100 results. Together these behaviours can
return a large set of entity-adjacent but answer-irrelevant memories. This design supersedes the
option-as-recall-text portion of
`2026-10-08-masm-v11-retrieval-relevance-recovery-design.md`; its other signal-preservation and
relation-safety requirements remain in force.

The score target for the next official Smoke run is at least 50. This is a target, not a guarantee
about hidden evaluation data. With 13 observed subchecks, moving from three passes to seven would
reach 53.85. The highest-leverage path is improving abstention while preserving atomic retrieval,
then recovering direct and multi-source questions through better evidence selection.

## Goals

1. Make the original question, not its answer options, determine candidate admission.
2. Use at most one bounded `gpt-4o-mini` call per Search request to select supporting evidence.
3. Return an empty successful result when the candidate pool does not support answering the query.
4. Preserve multiple independently sourced facts when a question requires a join, comparison,
   count, chronology, or other cross-request reasoning.
5. Keep original text and multimodal evidence unchanged in the public Search response.
6. Degrade to a deterministic shortlist instead of failing Search when selection is unavailable.
7. Add no new model, external provider, public schema, or heavy runtime dependency.

## Non-goals

- Do not generate the final benchmark answer in Search.
- Do not infer which multiple-choice option is correct.
- Do not inspect, store, or tune against hidden benchmark questions, answers, or memories.
- Do not redesign Add-time extraction, memory schemas, deletion, or asset lifecycle.
- Do not guarantee a particular hidden-evaluation score.
- Do not make a second selection attempt after a timeout or malformed response.

## Considered approaches

### Deterministic precision rules only

Remove option-driven admission, require meaningful lexical or semantic strength, diversify sources,
and cap the response. This has the lowest cost and keeps Search content local after embeddings, but
fixed thresholds cannot reliably distinguish entity overlap from sufficient evidence and are weak
on multi-source joins.

### Question-first recall plus one LLM evidence selector

Recall broadly from the original question, deterministically bound and diversify the pool, then ask
the already configured `gpt-4o-mini` provider to return only supported candidate indices. This adds
one bounded call and sends candidate text through the existing LLM provider, but it best addresses
the observed combination of poor abstention and multi-source reasoning. This is the selected
approach.

### Cross-encoder reranking

A local cross-encoder could improve pairwise relevance without generation, but it would enlarge the
image and CPU footprint, introduce another model lifecycle, and still require separate logic for
evidence sufficiency and multi-source selection. It is not selected for this release.

## Search pipeline

The Search path becomes:

1. analyze the original question into bounded question-only subqueries;
2. recall a broad internal pool through the existing lexical, text-vector, image-vector, and
   metadata channels;
3. apply question-only relevance admission;
4. expand relations only from admitted anchors;
5. deduplicate and deterministically rerank;
6. build a bounded, source-diversified selector pool;
7. make at most one structured LLM evidence-selection call;
8. validate selected indices and apply deterministic fallback when needed;
9. rehydrate selected items to their original text or multimodal representation;
10. enforce response count and byte limits.

`SearchRequest.options` remain available to the selector as auxiliary context. They never create a
query variant, satisfy the relevance gate, or add a ranking bonus. The selector is told that options
are untrusted alternatives and must select memories based on whether they support answering the
original question.

## Candidate admission and pool construction

The query analyzer may still decompose a complex original question, but it must not append option
text. `BaselineRetriever` continues to preserve raw per-channel signals. The relevance gate changes
its lexical rule from channel presence to meaningful evidence: a lexical candidate must have a
positive finite rank and pass a configurable minimum, or satisfy an existing strong semantic/image
criterion. Exact defaults will be chosen from deterministic proxy fixtures during implementation,
not from hidden runs.

After relation expansion and deterministic reranking, the selector receives no more than 32
candidates. Pool construction is deterministic and uses two passes:

- reserve candidates from distinct `request_id` values before allowing one request to dominate;
- fill remaining slots by deterministic rank.

`request_id` is the available provenance boundary for independently submitted Add units and is used
as the source-diversity key. This design does not add or fabricate session metadata. Ties are resolved
by the existing stable rank and memory id rules.

Each selector candidate contains:

- a zero-based opaque index;
- canonical text truncated to a configured per-candidate limit;
- granularity and source position;
- `request_id` represented as an opaque grouping label rather than raw diagnostic output;
- compact retrieval-signal labels and scores.

The selector does not receive raw image bytes. It receives the stored canonical text or description;
original media is rehydrated only after selection.

## Selector contract

Introduce a focused retrieval component, tentatively `EvidenceSelector`, backed by the existing
structured-output LLM client. Its strict response schema contains only:

- `selected_indices`: unique candidate indices in desired evidence order;
- `sufficient_evidence`: whether the selected candidates provide useful support for answering the
  original question.

The output has no answer, rationale, rewritten evidence, or arbitrary metadata. The prompt requires
the model to:

- select only evidence that directly supports the question;
- return no indices when memories merely share entities or option terms;
- include all independently sourced facts required for joins, comparisons, counts, and chronology;
- prefer original observations over summaries when both express the same fact;
- avoid selecting extra facts beyond what the question requires;
- never answer the question or choose an option.

Validation rejects duplicate, negative, out-of-range, non-integer, or over-limit indices. When
`sufficient_evidence` is false, the effective selection is empty even if indices were returned. A
valid non-empty selection is capped at 12 items. The cap leaves room for multi-source evidence while
keeping the downstream Answer context materially smaller than the external maximum of 100.

## Failure and fallback behaviour

Selection is attempted once. Provider unavailability, timeout, malformed structured output,
validation failure, or an empty response marked sufficient activates deterministic fallback rather
than an HTTP 503.

The fallback uses only question-derived signals. It keeps up to 12 top-ranked candidates that pass
the strengthened relevance gate, applies the same source-diversification rule, and returns an empty
result when none qualify. Options do not affect fallback admission. Existing response packing still
enforces the byte budget.

This fallback prioritizes service availability and preservation of current atomic retrieval. It is
not expected to equal the selector's abstention quality.

## Runtime wiring and configuration

The official runtime wires one selector instance with the existing `LLMClient` and configured
`gpt-4o-mini` model. The local-fake runtime uses a deterministic fake selector so tests never require
network access.

Configuration bounds include:

- selector enabled flag, enabled by default only for the official MASM profile;
- maximum selector candidates, default 32;
- maximum selected evidence, default 12;
- maximum canonical characters per candidate;
- selector timeout no greater than the existing model timeout.

Formal configuration validation prevents negative limits, selected limits larger than candidate
limits, unsupported official models, or an enabled official selector without an LLM provider.

## Privacy, observability, and safety

This change introduces a new data flow: Search candidate text, not just the query, is sent to the
already configured OpenAI-compatible LLM provider. The same provider is already used by the system,
but operators must be able to see that Search now uses it for evidence selection. No new provider is
introduced.

Logs and metrics contain only aggregate data:

- selector candidate and selected counts;
- whether fallback or abstention occurred;
- latency, token usage, model name, and sanitized provider failure category;
- number of distinct source groups before and after selection.

They must not contain questions, options, candidate text, raw identifiers, model responses, API
keys, or image payloads. Existing `user_id` scoping remains mandatory for every database recall and
relation expansion.

## Verification strategy

Implementation follows test-driven development. The initial failing tests cover:

1. options cannot create or admit candidates;
2. a direct fact survives recall, selection, rehydration, and packing;
3. entity-overlap-only candidates produce an empty response;
4. multi-source comparison selects required evidence from more than one `request_id`;
5. a list/count query keeps all necessary supported items without unrelated extras;
6. a multimodal hit is selected from canonical text and returned in original form;
7. invalid indices, contradictory sufficiency, timeout, and provider failure use deterministic
   fallback without returning 503;
8. selector and response caps are enforced deterministically;
9. logs and diagnostics contain no query, option, candidate, identifier, or provider body.

Tests use fake model responses and public synthetic fixtures only. Existing atomic, deletion,
isolation, response-byte, and original-evidence tests remain regression gates. Before deployment,
run the complete repository test suite, Ruff, mypy over the configured production/test targets, and
`git diff --check`.

## Deployment and evaluation

Deployment uses the existing immutable-image and config-override process. Pre-deployment checks must
record the source commit, clean tree, image id, current API container id, database container id, and
compose config paths. Replace only the API container and verify that the database id is unchanged.

After internal/external health checks, run one content-free functional probe covering positive
selection, abstention, and fallback. Clean up every probe record and verify zero residual rows and
assets. Then run one official v1.1 Smoke evaluation and compare only its published aggregate
metrics. Do not inspect hidden evaluation content or repeatedly tune against individual runs.

Rollback restores the previous immutable API image through the recorded compose configuration; the
database requires no migration and must remain unchanged.
