# MASM v1.1 Original Multimodal Evidence Design

**Date:** 2026-10-08

**Status:** Approved for native execution by the user's instruction to continue autonomously.

## Problem

Add stores immutable source messages and content-addressed image objects, but Search currently returns
only `Memory.summary`. For image questions, the fixed platform Answer stage therefore sees a caption
or dimension string rather than the original pixels. The public response schema already supports an
ordered list of text and image parts, so the loss happens inside MASM rather than at the API boundary.

## Goals

1. After relevance ranking, replace eligible message-memory summaries with their immutable original
   source message content.
2. Rehydrate stored image object keys into valid inline Data URLs while preserving content-part order.
3. Enforce user isolation, path confinement, maximum image size, and stored SHA-256 integrity before
   any object bytes enter a response.
4. Fall back to the ranked summary if source metadata or object bytes are missing, invalid, corrupt,
   or too large; Search availability must not depend on renderer success.
5. Keep response packing based on the actual rendered JSON size and never truncate an evidence item.

## Non-goals

- Do not alter source messages or assets.
- Do not render context memories that lack one unambiguous source position.
- Do not introduce remote image URLs or a new public endpoint.
- Do not change relevance scores, ranks, evidence ids, or official schemas.
- Do not make another model call.

## Design

### Batched source snapshots

`MemoryRepository.source_messages_for_positions` accepts a user id and at most 256 distinct
`(request_id, source_position)` keys. It performs user-scoped SQL queries for matching immutable
source messages and their referenced `Asset` rows. It returns `SourceMessageSnapshot` values whose
asset metadata includes object key, media type, decoded size, and content hash. Duplicate locations
are de-duplicated before SQL, and no result from another user can be represented.

### Safe object reads

`AssetStore.read_object` accepts an object key plus expected size/hash limits. It applies the same
relative-path, normalized-root, and real-path confinement checks as deletion; rejects missing,
non-file, oversized, or size-mismatched objects; reads the bytes; and verifies SHA-256. A dedicated
`AssetIntegrityError` identifies stored-object corruption without exposing paths or bytes.

### Evidence rendering

`RankedEvidence` gains internal provenance (`granularity`, `request_id`, `source_position`) and allows
its content to be either text or ordered `ContentPart` objects. `EvidenceRenderer` batches source
lookups for ranked message evidence, validates every stored part, and uses `dataclasses.replace` to
preserve id, rank, score, and all metadata while substituting original content.

For an image part, the renderer finds matching user-scoped asset metadata, reads and verifies the
object, and emits `data:<media_type>;base64,<payload>`. It treats the message atomically: if any part
cannot be validated or rehydrated, the entire evidence item stays as its ranked summary. Plain-text
source messages are restored exactly when non-blank.

Rendering runs after ranking and before `ResponsePacker`. The packer serializes the rendered content,
so Base64 expansion is included in the existing 30 MiB hard cap. An oversized rendered item is
skipped without hiding later smaller evidence.

### Runtime wiring

The application factory constructs one `EvidenceRenderer` from the same user-scoped repository and
`AssetStore` used by Add. It is enabled for every runtime profile because it is deterministic,
provider-free, and schema-compatible. Tests and internal callers may omit it to retain a minimal
SearchService.

## Safety and invariants

- Source and asset SQL filters include `user_id`.
- Object keys must be relative and remain inside the configured asset root both lexically and after
  real-path resolution.
- Stored bytes must not exceed the configured per-image limit and must match recorded size and hash.
- Only JPEG, PNG, and WebP metadata already accepted by Add may be emitted.
- A rendering failure never exposes a partial source message, object key, filesystem path, or error
  detail; the existing summary is retained.
- Public response ids, scores, order, top-k, and field names do not change.

## Verification

- Storage tests cover safe reads, traversal rejection, size mismatch, hash mismatch, and missing
  objects.
- Renderer tests cover ordered text-image-text restoration, exact plain text, summary fallback,
  unchanged non-message evidence, and user isolation.
- Integration tests exercise Add -> persistence -> Search and decode the returned Data URL to prove
  original image-byte equality.
- Packer tests prove rendered Base64 size is counted and later short evidence survives an oversized
  image item.
- Full gates: `pytest -q`, `ruff check .`, `mypy` for production and operational scripts, and
  `git diff --check`.

