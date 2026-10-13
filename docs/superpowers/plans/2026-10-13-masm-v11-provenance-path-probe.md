# MASM v1.1 Provenance Path Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test whether quote-backed, source-indexed claims distinguish wrong-subject chains from valid paraphrased multi-source answers.

**Architecture:** A standalone diagnostic asks the configured structured model to extract atomic claims without the question, validates every quote and mention against its own source, then asks a second model call to judge the question using only validated claims. It prints aggregate booleans and failure categories, never content. Add/Search/runtime remain unchanged.

**Tech Stack:** Python 3.11+, Pydantic v2, existing StructuredLLM, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-13-masm-v11-provenance-path-probe-design.md`

## Global Constraints

- Do not read, print, stage, or commit `秘钥.txt` or other secrets.
- Do not print raw source evidence, claims, provider output, logs, database rows, or credentials.
- Do not alter production Add/Search paths, database schema, deployments, or official evaluation gates.
- Do not require verbatim question identifiers, assume fixed two hops, or prune evidence using absent relationship edges.
- Real-model probe failure rejects the hypothesis; passing a tiny synthetic probe does not authorize deployment or paid Smoke.

## Review Focus

- Extracted quote belongs to a different source: reject the extraction without printing it.
- Extracted subject/object mention is absent from its quote: reject, including a plausible hallucinated alias.
- A first-person claim can represent the source speaker, but distinct source speakers are never equated automatically.
- Negative controls that share a workshop/route name without the same identity must remain unsupported.
- Provider exceptions or malformed output must produce a coarse category and no private text.

---

### Task 1: Quote-backed claim protocol

**Files:**
- Create: `scripts/provenance_path_probe.py`
- Test: `tests/unit/test_provenance_path_probe_script.py`

**Interfaces:**
- `Claim(source_index: StrictInt, quote: str, subject_mention: str, relation_mention: str, object_mention: str, subject_is_speaker: StrictBool)` and `ClaimExtraction(claims: tuple[Claim, ...])` are strict Pydantic models.
- `validate_claims(extraction: ClaimExtraction, sources: Sequence[str]) -> tuple[Claim, ...]` raises `ValueError` on invalid index, empty extraction, missing verbatim quote, missing subject/relation/object mention inside its own quote, or too many claims.
- `PathJudgement(question_subject_supported: StrictBool, all_required_links_supported: StrictBool, requested_value_supported: StrictBool, used_claim_indices: tuple[StrictInt, ...])` is a strict Pydantic model. A helper derives acceptance only when all booleans are true and the used indices are nonempty, distinct, and in range.

- [ ] **Step 1: Write failing tests** for valid multi-source speaker claims, wrong-source quote, fabricated mention, empty and oversized extraction, and invalid judgment indices.
- [ ] **Step 2: Run** `pytest -q tests/unit/test_provenance_path_probe_script.py` and confirm the new tests fail for missing protocol.
- [ ] **Step 3: Implement** the models and pure validators in `scripts/provenance_path_probe.py`; exact quote/mention checks compare only with source evidence, never with the question.
- [ ] **Step 4: Run** the focused tests and confirm they pass.
- [ ] **Step 5: Commit** these two files with an explicit path list.

### Task 2: Two-stage synthetic diagnostic

**Files:**
- Modify: `scripts/provenance_path_probe.py`
- Modify: `tests/unit/test_provenance_path_probe_script.py`

**Interfaces:**
- `run_probe(llm: StructuredLLM, settings: Settings, output: TextIO) -> int` sends selected evidence only to claim extraction; the judgment receives question and validated claims only.
- `main(argv: Sequence[str] | None = None) -> int` follows existing official-runtime diagnostic preflight and silences provider logging during the probe.

- [ ] **Step 1: Write failing tests** proving eight case names, positive/negative expected outcomes, stage separation, source-indexed claims, aggregate-only output, sanitized failures, and no database calls.
- [ ] **Step 2: Run** the focused tests and confirm failure for the absent probe behavior.
- [ ] **Step 3: Implement** two bounded model requests per case using the existing synthetic cases plus paraphrased multi-source; extract with no question, validate, then judge only quote-backed claims.
- [ ] **Step 4: Run** focused tests, `ruff check .`, and `mypy --explicit-package-bases src scripts/provenance_path_probe.py`.
- [ ] **Step 5: Run** full `pytest -q` and `git diff --check`; inspect the diff for data leaks and production changes, then commit explicit files only.

### Task 3: Real-model falsification, not deployment

**Files:** None unless the diagnostic itself has a verified defect.

- [ ] **Step 1: Push** the diagnostic branch through the local proxy after verifying the exact commit and clean worktree.
- [ ] **Step 2: Give** the user short, independent server commands to fetch the branch and run five `python -m scripts.provenance_path_probe` rounds against the existing API environment, without replacing or restarting any container.
- [ ] **Step 3: Evaluate** only the aggregate result. Any single positive rejection or negative acceptance means stop; no production integration or official Smoke in this stage.
