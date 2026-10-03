# MASM v1.1 双粒度记忆 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次 Add 同时保存可追溯的消息级记忆和整块上下文记忆，并用独立的 v1.1 环境验证检索收益。

**Architecture:** 复用现有 Add 编排、感知、Embedding、账本与事务，把消息级记忆作为同一 `MemoryBundle` 的后续成员；检索层保留来源元数据并控制父子重复。新版本使用独立数据库和资产卷，生产 v1.0 不迁移、不切流。

**Tech Stack:** Python、FastAPI、SQLAlchemy、Alembic、PostgreSQL/pgvector、pytest、Docker Compose、Caddy。

**Spec:** `docs/superpowers/specs/2026-10-03-masm-v11-dual-granularity-memory-design.md`

## Global Constraints

- v1.0 的 `submission-rc2`、线上 `v1.0` 和既有评测数据不得修改。
- 保持 `official-masm`、`gpt-4o-mini`、`text-embedding-v4`、1024 维；不得新增 LLM 调用。
- Add/Search 路由、认证、请求和响应 JSON 外形不变；`top_k <= 100`，响应上限 30 MiB。
- 所有写入、召回、治理和删除均按 `user_id` 隔离；不得记录官方请求正文、图片、密钥或个人标识明文。
- 消息记忆只作原文/忠实感知证据；不独立执行治理，也不生成最终答案。
- 每个任务先写失败测试、再实现、再跑相关回归并单独提交；不在本计划阶段改动服务器。

## Review Focus

- 空白文本与纯图片消息：跳过真正空消息，纯图片消息仍有可读证据和向量；Task 3 的 `test_empty_and_image_only_messages`。
- 同一消息内交错图文与多张图片：位置、描述、向量和资产引用不串位；Task 3 的 `test_interleaved_parts_keep_image_alignment`。
- 同一 `request_id` 的并发重试/接管：不会重复写入消息记忆或留下部分提交；Task 3 的 `test_dual_memory_replay_and_takeover`。
- 旧库迁移及旧记录：旧行可查询、可删除，部分唯一索引不误伤其他用户；Task 1 的 `test_0004_backfills_legacy_context`。
- `top_k=100` 与长上下文：64 个重排输入上限不会造成最多只返回 64 条，30 MiB 打包不被长父记忆垄断；Task 4 的 `test_top_100_with_parent_child_pool`。

---

### Task 1: 来源字段、唯一约束与迁移

**Files:**
- Create: `alembic/versions/0004_memory_granularity.py`
- Modify: `src/masm/storage/models.py`, `src/masm/storage/types.py`, `src/masm/storage/repositories.py`
- Test: `tests/integration/storage/test_migrations.py`, `tests/integration/test_add_transaction.py`, `tests/integration/test_delete_evaluation_run.py`

**Interfaces:**
- Consumes: 现有 `MemoryBundle(memories: Sequence[MemoryDraft])` 与 `MemoryRepository.finalize_request(...)`。
- Produces: `MemoryDraft.granularity: Literal["context", "message"] = "context"`、`MemoryDraft.source_position: int | None = None`；`Memory` 同名列；`MemoryCandidate.granularity: Literal["context", "message"] = "context"`、`.request_id: str = ""`、`.source_position: int | None = None`（使现有手工构造候选兼容），旧行默认 `context`。

- [ ] **Step 1: Write the failing tests**：`test_0004_backfills_legacy_context` 验证 `assert (row.granularity, row.source_position) == ("context", None)`，其他用户同名请求不冲突；`test_duplicate_message_position_rolls_back_bundle` 验证重复 `(user_id, request_id, source_position)` 后 `assert committed_rows == 0`；`test_delete_run_removes_both_granularities` 验证 `assert run_memory_count == 0`、`assert other_user_count == before`，并检查向量/来源/对象。
- [ ] **Step 2: Run tests to verify failure**：运行 `pytest tests/integration/storage/test_migrations.py tests/integration/test_add_transaction.py tests/integration/test_delete_evaluation_run.py -q`；新增测试应因缺列/约束失败。
- [ ] **Step 3: Implement storage change**：在 `0004` 增加非空默认 `context` 和可空 `source_position`，建立 `(user_id, request_id)` 的 context 部分唯一索引及 `(user_id, request_id, source_position)` 的 message 部分唯一索引；写入层校验 `context => None`、`message => 非负整数` 且 context 为 bundle 首项，治理动作仍只指向首项。
- [ ] **Step 4: Run tests to verify pass**：运行上述三个测试文件及 `pytest tests/integration/test_add_idempotency.py -q`；全部 PASS。
- [ ] **Step 5: Commit**：`git add alembic/versions/0004_memory_granularity.py src/masm/storage/models.py src/masm/storage/types.py src/masm/storage/repositories.py tests/integration/storage/test_migrations.py tests/integration/test_add_transaction.py tests/integration/test_delete_evaluation_run.py && git commit -m "feat: store memory granularity and source position"`。

### Task 2: 一次图片感知同时返回描述和向量

**Files:**
- Modify: `src/masm/providers/multimodal_embeddings.py`
- Test: `tests/unit/providers/test_multimodal_embeddings.py`

**Interfaces:**
- Consumes: `GroundedMultimodalEmbeddingProvider.embed_images(images: Sequence[bytes]) -> list[list[float]]`。
- Produces: 冻结数据类 `GroundedImageEmbedding(canonical_text: str, vector: list[float])`；`GroundedMultimodalEmbeddingProvider.ground_images(images: Sequence[bytes]) -> list[GroundedImageEmbedding]`。既有 `embed_images` 委托此方法，保持原契约。

- [ ] **Step 1: Write the failing tests**：`test_ground_images_preserves_order_and_single_perception_call` 验证 `assert perception_calls == len(images)` 和 `assert len(results) == len(images)`，描述/向量按输入顺序对应；`test_ground_images_rejects_mismatched_vectors` 验证向量数量不符抛 `ProviderResponseError`，不返回部分结果。
- [ ] **Step 2: Run tests to verify failure**：运行 `pytest tests/unit/providers/test_multimodal_embeddings.py -q`；新增测试应失败。
- [ ] **Step 3: Implement `ground_images`**：保留 `canonical_perception_text`；先按输入顺序生成描述，再批量 `embed_texts`，检查等长，返回描述/向量对；`embed_images` 只抽取其向量。
- [ ] **Step 4: Run tests to verify pass**：运行该测试文件及 `pytest tests/integration/test_runtime_profiles.py -q`；全部 PASS。
- [ ] **Step 5: Commit**：`git add src/masm/providers/multimodal_embeddings.py tests/unit/providers/test_multimodal_embeddings.py && git commit -m "feat: expose grounded image evidence without extra perception"`。

### Task 3: Add 中构造确定性消息记忆

**Files:**
- Create: `src/masm/services/message_memory.py`
- Modify: `src/masm/services/add_service.py`
- Test: `tests/unit/services/test_message_memory.py`, `tests/integration/test_add_idempotency.py`, `tests/integration/test_add_durability.py`, `tests/integration/test_agent_add_pipeline.py`

**Interfaces:**
- Consumes: Task 1 的 `MemoryDraft.granularity/source_position`；Task 2 的 `ground_images(...)`；现有 `SourceMessageDraft`、`EmbeddingDraft`、`MemoryBundle`。
- Produces: `build_message_memories(messages: Sequence[SourceMessageDraft], images_by_message: Sequence[Sequence[GroundedImageEmbedding]], text_vectors: Sequence[Sequence[float] | None], *, model_name: str, model_version: str) -> list[MemoryDraft]`；后两个序列均与 `messages` 等长，空消息的向量为 `None`；`AddService._build_bundle` 返回 `[context, *message_memories]`。

- [ ] **Step 1: Write the failing unit tests**：`test_one_add_yields_context_then_ordered_messages` 验证 `assert [(m.granularity, m.source_position) for m in memories] == [("context", None), ("message", 0), ("message", 1)]` 且原文不变；`test_empty_and_image_only_messages` 验证空消息跳过、纯图片摘要包含感知文本且有文本/图片向量；`test_interleaved_parts_keep_image_alignment` 验证图文分片、两张图片描述/向量/资产均对应原位置。
- [ ] **Step 2: Run unit tests to verify failure**：运行 `pytest tests/unit/services/test_message_memory.py -q`；应因新接口缺失失败。
- [ ] **Step 3: Implement message preparation/building**：`_prepare` 保持一次 context 摘要/向量；官方 Provider 调用 `ground_images`，其他现有 Provider 用 `embed_images` 和图片尺寸描述构造回退 `GroundedImageEmbedding`；一次批量 `embed_texts` 为所有非空消息（含纯图片消息的描述文本）生成文本向量，严格校验输出数量/顺序。把同一输入图片的既有图片向量附给对应消息，不再调用感知模型。
- [ ] **Step 4: Run unit tests to verify pass**：运行 `pytest tests/unit/services/test_message_memory.py tests/unit/providers/test_multimodal_embeddings.py -q`；全部 PASS。
- [ ] **Step 5: Write the failing integration tests**：`test_dual_memory_replay_and_takeover` 验证 `assert count_message_memories == count_nonempty_messages` 且重试前后 Provider 调用数相同；`test_image_mapping_failure_is_preclaim` 验证数量不符时 `assert ledger is None` 且无资产；`test_governance_actions_apply_only_to_context` 验证消息记忆无 relation/supersede。
- [ ] **Step 6: Run integration tests to verify failure**：运行 `pytest tests/integration/test_add_idempotency.py tests/integration/test_add_durability.py tests/integration/test_agent_add_pipeline.py -q`；新增测试应失败。
- [ ] **Step 7: Complete Add integration**：把消息级准备放在 claim 前，`_build_bundle` 和 `_degrade` 均提交同一双粒度束；不改变 Add JSON、fencing、锁和失败码。
- [ ] **Step 8: Run integration tests to verify pass**：运行上述三个集成测试文件及 `pytest tests/contract/test_api_schemas.py -q`；全部 PASS。
- [ ] **Step 9: Commit**：提交本任务两个源码文件和四个测试文件，消息 `feat: write source-aligned message memories atomically`。

### Task 4: 来源感知的召回、去重与 Top K

**Files:**
- Modify: `src/masm/storage/repositories.py`, `src/masm/retrieval/baseline.py`, `src/masm/retrieval/reranker.py`, `src/masm/providers/reranker.py`, `src/masm/services/search_service.py`
- Test: `tests/unit/retrieval/test_baseline_recall.py`, `tests/unit/retrieval/test_conflict_reranking.py`, `tests/unit/retrieval/test_response_packer.py`, `tests/integration/test_retrieval_enhancements.py`

**Interfaces:**
- Consumes: Task 1 的 `MemoryCandidate.granularity/request_id/source_position`；现有 `ParsedQuery`、`RankedEvidence`。
- Produces: `MemoryRepository.context_candidates_for_requests(user_id: str, request_ids: Sequence[str]) -> list[MemoryCandidate]` 和 `MemoryRepository.message_candidates_for_requests(user_id: str, request_ids: Sequence[str], limit: int) -> list[MemoryCandidate]`（均严格同用户）；`EvidenceReranker.rank(query: ParsedQuery, candidates: Sequence[MemoryCandidate]) -> list[RankedEvidence]` 最多返回 100 个候选；Provider 评分输入仍最多 64 条；`SearchService.search(request: SearchRequest) -> SearchResponse` 的外形不变。

- [ ] **Step 1: Write the failing tests**：`test_channels_preserve_provenance` 验证四通道候选的 `(granularity, request_id, source_position)` 不丢；`test_message_precedes_same_run_context` 验证 `assert rank(message) < rank(context)`；`test_parent_child_dedup_preserves_relation_anchor` 覆盖父子重复与跨 session 关系；`test_top_100_with_parent_child_pool` 验证 `assert len(result.data) == 100`、`assert provider_input_count <= 64`、响应不超过 30 MiB；`test_legacy_context_and_user_isolation` 验证旧行可检索且 `assert all(row.user_id == requested_user for row in candidates)`。
- [ ] **Step 2: Run tests to verify failure**：运行上述四个测试文件；新增测试应失败。
- [ ] **Step 3: Implement bounded retrieval/ranking**：所有 repository 候选构造与 RRF 融合均传递三项来源字段；Search 取至多 256 个原始候选，用消息候选的同源 context 作关系扩展锚点，并有界补入相关运行的消息候选；按来源位置去重并控制每次 Add 的 context 数量，再重排。保留冲突组准入；Provider 只评前 64 个，其余用确定性分数排序，最多保留 100 个给 `ResponsePacker`；不截断单条证据，不返回生成答案。
- [ ] **Step 4: Run tests to verify pass**：运行上述四个测试文件和 `pytest tests/contract/test_api_schemas.py -q`；全部 PASS，核对现有 64 上限测试改为“Provider 输入 ≤64、最终证据 ≤100”。
- [ ] **Step 5: Commit**：提交本任务源码和测试，消息 `feat: rank source-aligned evidence through top 100`。

### Task 5: 脱敏诊断与可重复的本地比较

**Files:**
- Create: `src/masm/retrieval/diagnostics.py`, `scripts/evaluate_synthetic_recall.py`, `tests/unit/retrieval/test_diagnostics.py`, `tests/integration/test_synthetic_recall.py`
- Modify: `src/masm/retrieval/baseline.py`, `src/masm/services/search_service.py`, `src/masm/api/app.py`, `src/masm/api/routes.py`, `deployments/README.md`

**Interfaces:**
- Consumes: Task 4 的候选、重排、打包计数；公开 Add/Search HTTP 契约。
- Produces: `BaselineRetriever.retrieve_with_stats(user_id: str, query: ParsedQuery, limit: int) -> tuple[list[MemoryCandidate], Mapping[str, int]]`（现有 `retrieve` 委托并只返回候选）；`SearchDiagnostics(request_tag: str, runtime_profile: str, candidate_count: int, dedup_count: int, returned_count: int, response_bytes: int, latency_ms: float, status_code: int, channel_counts: Mapping[str, int])`；`emit_search_diagnostics(value: SearchDiagnostics) -> None`，只输出这些字段；脚本产出按能力类别的 JSON 指标。

- [ ] **Step 1: Write the failing tests**：`test_diagnostics_never_logs_payload_or_identity` 捕获日志并验证 `assert not any(secret in log_text for secret in (user_id, query, content, image_data, api_key))`；`test_synthetic_recall_by_category` 用固定语料/种子覆盖直接召回、单消息多事实、跨 session、图文、纯图片、无证据、100 候选和响应上限，验证每类 Recall@10/100、覆盖率及空结果可复算。
- [ ] **Step 2: Run tests to verify failure**：运行 `pytest tests/unit/retrieval/test_diagnostics.py tests/integration/test_synthetic_recall.py -q`；新增测试应失败。
- [ ] **Step 3: Implement diagnostics and evaluation**：检索器返回每通道数量；Search 成功时记录聚合计数，API 错误路径只记录状态码/耗时，`request_tag` 使用随机标识且不由 `user_id` 推导；沿用 Compose `json-file` 的 `10m × 5` 轮转。脚本接收 v1.0/v1.1 URL 与各自 Key（环境变量，不打印），输出分能力指标和平均/P95 Add/Search 延迟；Embedding 输入量、LLM/感知调用数及成本只在本地可观测时填写，否则明确标注 `unavailable`，不猜测。
- [ ] **Step 4: Run tests to verify pass**：运行上述两个测试文件及 `pytest tests/contract tests/integration/test_add_search_baseline.py -q`；全部 PASS；用两套本地服务运行脚本，保存仅含聚合数值的比较报告。
- [ ] **Step 5: Commit**：提交本任务源码、测试和文档，消息 `test: measure dual-granularity retrieval without payload logs`。

### Task 6: 独立 v1.1 部署包与发布门槛

**Files:**
- Create: `deployments/docker-compose.v11.yml`, `docs/competition/masm-v11-release-checklist.md`
- Modify: `docker-compose.yml`, `deployments/Caddyfile`, `deployments/README.md`
- Test: `tests/integration/test_runtime_profiles.py`, `tests/integration/test_delete_evaluation_run.py`

**Interfaces:**
- Consumes: v1.1 API 和既有 `/health`、Add/Search、删除脚本；Task 5 的比较报告。
- Produces: 独立 Compose 项目 `masm-v11`（独立 Postgres/资产卷，不启动第二个 Caddy），共享外部网络 `masm-edge`；Caddy 的 `v11.agentmemorydev.icu` 新站点指向别名 `masm-v11-api:8000`，旧站点 `agentmemorydev.icu` 仍指向 `api:8000`。

- [ ] **Step 1: Write the failing deployment checks**：`docker compose -p masm-v11 -f deployments/docker-compose.v11.yml config -q` 应因文件缺失失败；新增测试断言 official profile 仍是指定模型/1024 维、双粒度运行级删除完整。
- [ ] **Step 2: Run tests to verify failure**：运行上述 `docker compose ... config -q` 和两个集成测试文件中的新增用例；配置与新增用例应失败。
- [ ] **Step 3: Implement packaging and checklist**：新 Compose 仅启动 API/Postgres，资产和数据库卷独立，API 内网别名固定；旧 Compose 仅让现有 Caddy 加入 `masm-edge`，不改 v1.0 API 网络/卷/路由；Caddy 新增第二站点。清单写明备份、DNS、两域名 Health、迁移、公共 Smoke、401、16/16、容量与成本、回滚，且官方 `Add Version`/Smoke 须在用户审阅结果后执行。
- [ ] **Step 4: Verify packaging**：运行 `docker compose config -q`、`docker compose -p masm-v11 -f deployments/docker-compose.v11.yml config -q`、`caddy validate --config deployments/Caddyfile --adapter caddyfile`（若本地无 Caddy，用 `caddy:2.8-alpine` 容器验证）；运行 `pytest tests/integration/test_runtime_profiles.py tests/integration/test_delete_evaluation_run.py -q`，全部 PASS。只在独立本地环境启动 v1.1，验证两套 Health、Add/Search/删除和重启持久性；生产部署是后续单独步骤。
- [ ] **Step 5: Commit**：提交本任务配置、测试和清单，消息 `chore: package isolated v11 deployment and release gates`。

## 完成判据

全量 `pytest -q`、迁移、合成集和独立部署检查均通过；比较报告按能力类别证明证据覆盖提升，同时记录延迟、调用量和成本。若本地证据不支持改进或违反无新增 LLM 调用约束，则停止在本地诊断，不创建线上 v1.1 版本、不耗用官方 Smoke。
