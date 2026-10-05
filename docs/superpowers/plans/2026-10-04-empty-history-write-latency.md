# Empty-History Text Write Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an opt-in single-call Perception+Temporal path for empty-history text Add, then measure whether it reduces actual local latency without losing temporal fields.

**Architecture:** A new fused agent produces an existing perception schema plus only persisted time fields and exact input evidence. AddPipeline selects it only for text with no context history, retains recall and race-safe temporal fallback, and preserves all other routes. Runtime config defaults off; metered holdout reports the new stage separately.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI, SQLAlchemy, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-empty-history-write-latency-design.md`

## Global Constraints

- Reuse `E:/Competitions/AgentMemoryChallenge/.worktrees/masm-v11`; preserve all existing uncommitted work.
- No cloud/production database, official evaluation, capacity test, push, or key output.
- Feature flag defaults off; no change to public Add/Search JSON.
- Do not commit during this user-directed local validation phase.

## Review Focus

- A date expressed in text must survive the fused path as event_time/time_precision.
- History appearing between preflight and recall must use legacy temporal analysis.
- Invalid fused output must not silently discard a grounded time.
- Mixed/image content must never go to text-only fusion.
- Missing Provider must retain existing degradation semantics.

---

### Task 1: Fused structured agent

**Files:** Create `src/masm/agents/fused_text.py`, `src/masm/agents/prompts/fused_text_v1.txt`, `tests/unit/agents/test_fused_text_agent.py`; modify `src/masm/schemas/agents.py`.

**Interfaces:** Produce `FusedTextResult(perception: PerceptionResult, event_time: datetime | None, time_precision: TimePrecision, time_evidence: str | None)` and `FusedTextAgent.extract(content: Sequence[TextPart]) -> FusedTextResult`.

- [ ] Write tests for schema, ordered original text, prompt fidelity, exact time evidence, precision consistency, and date preservation.
- [ ] Run the new tests and observe feature-missing failures.
- [ ] Implement the minimal agent/schema/prompt and run tests to green.

### Task 2: Safe pipeline selection and runtime flag

**Files:** Modify `src/masm/orchestration/add_pipeline.py`, `src/masm/runtime.py`, `src/masm/config.py`; test `tests/unit/test_runtime_settings.py`, `tests/unit/test_runtime_factory.py`, `tests/integration/test_agent_add_pipeline.py`.

**Interfaces:** Consume `FusedTextAgent`; `AddPipeline(..., fused_text: FusedTextAgent | None = None)`. `Settings.fused_empty_history_text: bool = False`, environment `MASM_FUSED_EMPTY_HISTORY_TEXT=1` only enables it.

- [ ] Add failing tests for off/on, empty history text, dated text, image/history exclusion, invalid-output fallback, and post-preflight history race.
- [ ] Run focused tests to confirm failure due to missing selection.
- [ ] Implement selection, fallback and config; run focused tests to green.

### Task 3: Metering, full verification, bounded pilot, report

**Files:** Modify `scripts/metered_holdout.py`, `tests/unit/experiments/test_metered_holdout.py`, Chinese report/handoff/checklist.

**Interfaces:** `fused_text` stage and `fused_text_calls` count, no request content in persisted metrics.

- [ ] Add failing metering test, then implement and verify it.
- [ ] Run Ruff, mypy, database integration and full pytest with local `masm_test` only.
- [ ] If all green, run bounded fixed-input local pilot with existing Provider credentials and exact cleanup; compare with baseline and explicit date case.
- [ ] Document measured facts versus pending checks; leave flag off unless evidence is strong.

Plan ruling: The user asked not to pause for routine choices; execute inline without additional design-approval prompts. No commits are made because the existing uncommitted work must be preserved and this remains a local validation phase.

Pilot ruling: The first fixed three-case pilot of the full nested TemporalRelationResult produced one grounding rejection and fallback, increased tokens, and did not establish meaningful latency gain. Simplify the fused output to the only persisted time fields, then re-run offline tests and a bounded pilot; keep default off.
