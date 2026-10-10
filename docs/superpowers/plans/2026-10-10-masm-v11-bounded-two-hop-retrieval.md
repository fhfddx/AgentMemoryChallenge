# MASM v1.1 Bounded Two-Hop Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement
> this plan task by task in the current session. Use `superpowers:test-driven-development` for
> every behavior change and `superpowers:verification-before-completion` before any completion
> claim. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a question-admitted memory anchor retrieve a bounded explicit `A -> B -> C`
cross-session chain without admitting disconnected memories or changing evidence-selector-v7.

**Architecture:** Keep the existing Search pipeline and repository contract. Extend
`RelationExpander` from one persisted relation hop to at most two. Conflict peers retain priority,
first-hop neighbors provide at most eight traversal seeds, at most eight second-hop candidates may
enter the result, and the shared expansion cap remains 32. Search continues to hydrate message
evidence for every expanded context before the unchanged reranker and selector.

**Tech Stack:** Python 3.11+, dataclasses, FastAPI/Pydantic, SQLAlchemy/PostgreSQL, pytest, Ruff,
mypy.

**Spec:** `docs/superpowers/specs/2026-10-10-masm-v11-bounded-two-hop-retrieval-design.md`

**Worktree:** `C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge`

**Branch:** `codex/masm-v11-multihop`, based on `65738a44b181`.

## Global Constraints

- Do not modify `src/masm/retrieval/evidence_selector.py`, selector prompts, selector schemas,
  selector pool limits, or fallback behavior.
- Follow only persisted memory-to-memory relations. Similarity, shared words, options, or model
  guesses must not create graph edges.
- Keep every repository read scoped by `user_id` in SQL.
- Preserve `MAX_SEEDS = 8`, `MAX_EXPANDED = 32`, conflict priority, deterministic order, public
  Add/Search schemas, response limits, and database schema.
- Add no new provider request and no database migration.
- Do not use hidden evaluation content or raw production logs.
- Do not push, deploy, or start a paid evaluation without the user's explicit instruction.

## Review Focus

- The new connected test must fail on the frozen one-hop baseline before production code changes.
- A second hop must not turn a disconnected or merely similar memory into evidence.
- `A -> B -> A`, reciprocal edges, repeated edges, and diamond graphs must emit every memory at
  most once.
- Conflict peers must not be displaced by relation traversal.
- If the first hop is empty, the repository must not receive a second-hop query.
- A second-hop context must follow the existing request-scoped message rehydration path.
- Selector files and expectations must remain byte-for-byte unchanged on this branch.

---

### Task 1: Pin the one-hop defect with failing relation tests

**Files:**
- Modify: `tests/unit/retrieval/test_relation_expander.py`

**Interfaces under test:**
- Existing: `RelationExpander.expand(user_id: str, seeds: Sequence[MemoryCandidate], limit: int)`.
- Planned constant: `MAX_SECOND_HOP = 8`.

- [ ] **Step 1: Read the required test-quality guidance**

Read `superpowers/test-driven-development/writing-good-tests.md` completely before editing tests.

- [ ] **Step 2: Replace the obsolete one-hop assertion with a connected two-hop requirement**

Rename `test_expands_exactly_one_hop` to
`test_expands_connected_chain_through_two_hops`. Build adjacency `A: [B]`, `B: [C]`, and assert
the result IDs are `[B, C]` and repository relation calls are `[[A], [B]]`.

- [ ] **Step 3: Add the paired disconnected negative case**

Create `D` with similar content but no edge. Assert an `A -> B -> C` traversal emits `[B, C]` and
never emits `D`. Verify repository calls contain IDs only from the connected graph.

- [ ] **Step 4: Run the focused connected test and verify RED**

Run:

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/retrieval/test_relation_expander.py::test_expands_connected_chain_through_two_hops -q
```

Expected: FAIL because the baseline returns only `B` and makes one relation call. If it passes,
stop and reassess because the test is not exercising the frozen defect.

- [ ] **Step 5: Run the disconnected test separately**

Expected: the exclusion assertion may already pass. Record it as the paired safety baseline; do not
weaken it to manufacture a red test.

- [ ] **Step 6: Commit tests only**

```powershell
git add tests/unit/retrieval/test_relation_expander.py
git commit -m "test: expose missing two-hop relation evidence"
```

### Task 2: Implement bounded two-hop relation expansion

**Files:**
- Modify: `src/masm/retrieval/relation_expander.py`
- Modify: `tests/unit/retrieval/test_relation_expander.py`

**Interfaces:**
- Preserve: `RelationExpander.expand(...) -> list[MemoryCandidate]`.
- Add internal constant: `MAX_SECOND_HOP = 8`.
- Add small private helpers only when they make hop collection, de-duplication, or quota allocation
  independently testable; do not widen the public surface.

- [ ] **Step 1: Add remaining failing unit cases before implementation**

Add tests for:

- reciprocal cycle `A -> B -> A` returning only `B`;
- diamond `A -> B/C -> D` returning `D` once;
- empty first hop making exactly one `related()` call;
- at most eight second-hop traversal seeds;
- at most eight emitted second-hop candidates;
- total output respecting both the caller limit and `MAX_EXPANDED`;
- a budget of one preserving a first-hop bridge rather than returning a disconnected endpoint;
- conflict peers retaining the first output slots and their conflict-group identity.

- [ ] **Step 2: Run the whole relation-expander test module and verify RED**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/retrieval/test_relation_expander.py -q
```

Expected: the connected, cycle-call-count, second-hop, and quota cases fail against the one-hop
implementation; existing safety tests remain green.

- [ ] **Step 3: Implement the minimum two-hop algorithm**

In `relation_expander.py`:

1. update module/class/method documentation from one hop to at most two;
2. bound and de-duplicate original seeds;
3. collect conflict peers exactly as before;
4. query first-hop neighbors once with the bounded budget;
5. select the first eight deterministic, non-seed first-hop candidates as traversal seeds;
6. query at most eight second-hop candidates once, only when traversal seeds exist;
7. remove original seeds and all duplicate IDs across conflicts/hops;
8. reserve at most eight remaining slots for second-hop results while retaining at least one
   first-hop result when one exists;
9. emit conflict peers, first-hop quota, then second-hop quota, never exceeding the shared budget.

Do not change `MemoryRepository.related()` or issue unscoped reads.

- [ ] **Step 4: Run relation-expander tests and verify GREEN**

Use the Task 2 Step 2 command. Expected: all relation-expander tests pass.

- [ ] **Step 5: Run isolation and immediate Search regressions**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/isolation/test_relation_user_isolation.py tests/integration/test_retrieval_enhancements.py -q
```

Expected: zero failures.

- [ ] **Step 6: Commit the implementation**

```powershell
git add src/masm/retrieval/relation_expander.py tests/unit/retrieval/test_relation_expander.py
git commit -m "feat: expand bounded two-hop memory relations"
```

### Task 3: Prove Search rehydrates second-hop source evidence

**Files:**
- Modify: `tests/integration/test_retrieval_enhancements.py`
- Modify only if the red test proves necessary: `src/masm/services/search_service.py`

**Interfaces under test:**
- `SearchService._with_expansion(user_id, candidates, top_k)`.
- Existing repository methods `context_candidates_for_requests(...)` and
  `message_candidates_for_requests(...)`.

- [ ] **Step 1: Write a Search-level connected test**

Use a recording repository and the real `RelationExpander`. Recall a message-level anchor from
request `run-a`; map it to context `A`; define relations `A -> B -> C`; define original
message evidence for `run-b` and `run-c`. Inject the deterministic selector and assert Search
returns evidence originating from both expanded requests, including the second-hop `run-c`
message, without returning context/message duplicates.

- [ ] **Step 2: Write a Search-level disconnected test**

Add an unconnected context/message pair `D` with similar text. Assert neither representation of
`D` is returned.

- [ ] **Step 3: Run the two new tests and verify their baseline signal**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/integration/test_retrieval_enhancements.py -k "second_hop or disconnected_relation" -q
```

Expected after Task 2: the connected and disconnected cases should pass through existing Search
rehydration. If the connected case fails, preserve the failure output and make only the minimum
SearchService correction. Do not modify selector code.

- [ ] **Step 4: If required, implement the smallest SearchService fix and rerun GREEN**

The permitted correction is limited to collecting `request_id` values from all expanded contexts
and loading their message candidates once. Do not change ranking, selector invocation, fallback,
or response packing.

- [ ] **Step 5: Run selector non-regression tests**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/retrieval/test_evidence_selector.py tests/unit/retrieval/test_selector_pool.py tests/unit/test_selector_probe_script.py tests/unit/test_selector_reasoning_probe_script.py -q
```

Expected: zero failures, with no selector source changes.

- [ ] **Step 6: Commit Search-level tests and any proven minimal fix**

```powershell
git add tests/integration/test_retrieval_enhancements.py src/masm/services/search_service.py
git commit -m "test: cover second-hop evidence rehydration"
```

If `search_service.py` was unchanged, omit it from `git add`.

### Task 4: Add a content-safe deployed multi-session probe

**Files:**
- Create: `scripts/multi_session_path_probe.py`
- Create: `tests/unit/test_multi_session_path_probe_script.py`

**Interfaces:**
- Follow `scripts/search_path_probe.py` for loopback-only URL validation, environment-only API key
  access, exact registered-run cleanup, safe JSON rows, and exit codes `0/1/2`.
- Probe cases use random synthetic markers only and never print content, identifiers, credentials,
  object URIs, or model bodies.

- [ ] **Step 1: Write failing probe tests**

Use fake clients/repositories/deletion reports to assert:

- three sequential Add requests are registered before transmission;
- a connected chain requires all registered marker classes in Search output;
- a disconnected lookalike is not accepted as support;
- request/contract failures emit only a fixed failure category;
- cleanup always runs, retries, and proves zero residual memories/sources;
- output keys are a fixed safe allowlist;
- non-loopback base URLs fail preflight without making requests.

- [ ] **Step 2: Run probe tests and verify RED**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/test_multi_session_path_probe_script.py -q
```

Expected: import or behavior failure because the probe does not yet exist.

- [ ] **Step 3: Implement the probe by reusing the established safety pattern**

Create an isolated user and three request/session IDs. Add a synthetic `Root -> Bridge`,
`Bridge -> Leaf`, and `Leaf -> terminal fact` sequence. Search from the root with a chain question,
run a disconnected-chain negative case, print counts/booleans only, and perform three exact cleanup
rounds in `finally`.

- [ ] **Step 4: Run probe tests and existing probe regressions**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/test_multi_session_path_probe_script.py tests/unit/test_search_path_probe_script.py -q
```

Expected: zero failures.

- [ ] **Step 5: Commit the probe**

```powershell
git add scripts/multi_session_path_probe.py tests/unit/test_multi_session_path_probe_script.py
git commit -m "test: add safe multi-session path probe"
```

### Task 5: Full verification and release handoff

**Files:**
- Modify: `docs/competition/masm-v11-deepseek-harness-handoff-2026-10-10.md`
- Review only: `src/masm/retrieval/evidence_selector.py`

- [ ] **Step 1: Review branch scope before running broad gates**

Run:

```powershell
git diff 65738a44b181 -- src/masm/retrieval/evidence_selector.py
git diff --stat 65738a44b181..HEAD
git status --short
```

Expected: no selector diff; only the relation expander, focused Search test/fix if proven, safe probe,
tests, and documentation are changed; worktree is clean before final documentation edits.

- [ ] **Step 2: Run focused capability and safety gates**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest tests/unit/retrieval/test_relation_expander.py tests/isolation/test_relation_user_isolation.py tests/integration/test_retrieval_enhancements.py tests/unit/test_multi_session_path_probe_script.py tests/unit/test_search_path_probe_script.py tests/unit/retrieval/test_evidence_selector.py tests/unit/test_selector_probe_script.py tests/unit/test_selector_reasoning_probe_script.py -q
```

Expected: zero failures.

- [ ] **Step 3: Run the complete repository suite**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m pytest -q
```

Expected: zero failures. Record the actual passed/skipped/warning counts; do not reuse an older
count.

- [ ] **Step 4: Run static and whitespace gates**

```powershell
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m ruff check .
& 'E:\Competitions\AgentMemoryChallenge\.venv\Scripts\python.exe' -m mypy src scripts/selector_probe.py scripts/selector_reasoning_probe.py scripts/compare_search_diagnostics.py scripts/search_path_probe.py scripts/multi_session_path_probe.py scripts/cloud_smoke_recovery.py
git diff --check 65738a44b181..HEAD
```

Expected: all commands exit zero and `git diff --check` prints nothing.

- [ ] **Step 5: Update the handoff with evidence and exact deployment gates**

Record the branch commits, actual verification counts, selector no-diff proof, new probe contract,
immutable-image/API-only deployment procedure, unchanged-DB requirement, rollback to `84926f6`, and
the rule that no official Smoke starts before deployed old/new synthetic gates pass. Do not include
keys, environment values, raw logs, hidden content, or a predicted score.

- [ ] **Step 6: Commit the verified handoff**

```powershell
git add docs/competition/masm-v11-deepseek-harness-handoff-2026-10-10.md
git commit -m "docs: hand off bounded multi-session candidate"
```

- [ ] **Step 7: Apply verification-before-completion**

Re-run `git status --short`, `git log --oneline 65738a44b181..HEAD`, the exact focused tests affected
by the final documentation commit where applicable, and `git diff --check 65738a44b181..HEAD`.
Report evidence, remaining risks, and manual push/deployment commands. Do not push or deploy.
