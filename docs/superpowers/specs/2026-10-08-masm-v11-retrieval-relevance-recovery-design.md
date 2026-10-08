# MASM v1.1 Retrieval Relevance Recovery Design

**Date:** 2026-10-08

**Status:** Approved for native execution by the user's instruction to continue autonomously.

## Problem

The completed official multimodal Smoke run scored 11.54. The public result reports useful
atomic retrieval but zero direct recall, multi-session reasoning, and abstention. The current
implementation explains several of those symptoms without relying on hidden evaluation data:

- nearest-neighbour retrieval always yields a candidate, even when every absolute similarity is
  weak;
- weighted reciprocal-rank fusion discards the raw lexical/vector evidence that a later stage
  would need to abstain;
- a fixed `0.05` message bonus is larger than a typical rank-one RRF contribution and can promote
  a weak message over a stronger context;
- query analysis can create several focused queries, but recall concatenates them back into one
  PostgreSQL query and one embedding;
- `SearchRequest.options` are validated but never influence retrieval.

This sub-project fixes those retrieval and abstention defects only. Original multimodal evidence
rehydration is a separate sub-project because it changes storage-to-response rendering and byte
accounting rather than relevance selection.

## Goals

1. Preserve absolute per-channel retrieval evidence through RRF without changing the public API.
2. Execute focused text queries independently, then merge results once per logical channel so a
   memory cannot gain weight merely by appearing in several equivalent variants.
3. Let every supplied option participate symmetrically in recall; no option is treated as a label
   or presumed answer.
4. Reject a recalled pool before relation expansion when it has neither a lexical hit nor a
   sufficiently strong vector hit.
5. Keep relation-expanded evidence only when expansion starts from a strong admitted anchor.
6. Make message granularity an exact-score tie-break, not an additive relevance signal.

## Non-goals

- Do not generate or rewrite answers in `/search`.
- Do not inspect hidden evaluation data or tune against individual official questions.
- Do not change the official Add/Search request or response schemas.
- Do not add a second model call or a new external dependency.
- Do not redesign write-time memory curation, atomic fact storage, or the relation schema here.

## Design

### Query variants

`SearchService` first obtains the normal `ParsedQuery`. It then appends bounded, de-duplicated
option-aware variants of the form `<original textual question>\nCandidate: <option>`, one for each
option, preserving option order. The global variant cap is eight, which remains within the known
embedding provider batch limit of ten. Empty and duplicate variants are removed by normalized
case-insensitive comparison.

`BaselineRetriever` embeds all text variants in one provider call. It runs lexical and text-vector
recall for each variant independently. Results are then merged into one ranked list per logical
channel (`lexical`, `text_vector`, and optionally `image_vector`) by keeping each memory's best raw
score and using memory id as the deterministic final tie-break. RRF sees each logical channel once.

### Retrieval signals

`MemoryCandidate` gains an immutable `retrieval_signals: Mapping[str, float]` field. Repository
methods remain simple raw-channel producers. `BaselineRetriever` fills the field after merging:

- `lexical`: the best PostgreSQL text-search rank;
- `text_vector`: the best cosine similarity;
- `image_vector`: the best cosine similarity;
- `metadata`: the best metadata channel score.

The existing `score` field remains the fused RRF score, so downstream ordering contracts remain
compatible. Reranking copies these signals into private `RankedEvidence.metadata`; none are exposed
as new public response fields.

### Relevance gate

`RelevanceGate` filters only the initially recalled candidates. A candidate is a strong anchor if:

- it appeared in the lexical channel; or
- its best text-vector similarity is at least `0.48`; or
- its best image-vector similarity is at least `0.42`.

Thresholds are finite values in `[-1, 1]`, configurable as
`MASM_MIN_TEXT_SIMILARITY` and `MASM_MIN_IMAGE_SIMILARITY`. Defaults are deliberately conservative
first-pass cutoffs, not claims about hidden evaluation distributions. Metadata-only hits and RRF
rank alone are not sufficient. The gate runs before relation expansion, so no unrelated seed can
pull in an evidence bundle; once an anchor is admitted, its bounded relation expansion remains
eligible because relation edges provide provenance distinct from raw similarity.

The local-fake baseline keeps the gate disabled by default to preserve existing development and
contract-test behaviour. `official-masm` enables it. Tests can inject it explicitly.

### Ranking

Remove the additive message bonus from admission and scoring. When two evidence items have the same
final score, prefer `message` over `context`, then sort by memory id. Entity, time, relation,
conflict, duplicate, and optional provider adjustments remain unchanged.

## Safety and invariants

- Every database recall remains scoped by `user_id` in SQL.
- Variant count, provider calls, and candidate pools remain bounded.
- The public schema and `top_k <= 100` contract do not change.
- Options are used only as retrieval text and are never logged in diagnostics.
- Diagnostics remain aggregate counts and latency only.
- Relation expansion cannot begin from a candidate rejected by the relevance gate.
- Empty admissible evidence is a successful `200` response with `{"data": []}`.

## Verification

- Unit tests prove separate subquery calls, logical-channel de-duplication, signal preservation,
  option symmetry/capping, gate admission/rejection, and exact-score message tie-breaking.
- Integration tests prove unrelated stored history yields an empty response while exact lexical,
  strong semantic, and relation-expanded evidence survive.
- Full regression gates: `pytest -q`, `ruff check .`, `mypy` over production and touched scripts,
  and `git diff --check`.

