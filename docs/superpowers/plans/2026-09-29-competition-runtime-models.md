# MASM 正式模型与比赛运行链路实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MASM 具备可审计的本地 Fake、官方基线和官方完整 MASM 三种运行档位，并能在注入合法密钥后调用 `gpt-4o-mini` 与 `text-embedding-v4`。

**Architecture:** 用独立 Runtime Factory 根据运行档位构造 Provider、AddPipeline 和 Search 增强组件；正式多模态图片先经 `gpt-4o-mini` 结构化，再统一进入 `text-embedding-v4` 向量空间。公共 Add/Search Schema、存储事务和用户隔离保持不变。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、HTTPX、PostgreSQL/pgvector、Pytest、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-09-29-competition-runtime-models-design.md`

## Global Constraints

- 正式 LLM 模型必须为 `gpt-4o-mini`，正式 Embedding 模型必须为 `text-embedding-v4`；Reranker 初始使用确定性 `LexicalReranker`。
- `local-fake` 不需要密钥、不访问公网；正式档位缺配置必须在接收请求前失败，禁止静默退回 Fake。
- 文本和图片必须进入同一 `text-embedding-v4` 向量空间；正式档位不得生成 Fake 向量。
- 模型密钥、请求原文、Base64、Prompt 载荷和供应商响应正文不得进入日志、异常文本或实验 manifest。
- Provider 尝试次数只能是 1 或 2；测试只能使用 Fake 或 `httpx.MockTransport`。
- `/add` 和 `/search` 契约保持不变，Search 只能返回证据。
- Embedding 失败返回 HTTP 503，且不得遗留 PROCESSING 账本或暂存对象。
- 所有代码和测试文档优先使用中文；厂商字段、模型名和 API 字段保留英文。

## Review Focus

- Embedding 响应 index 乱序、缺失或重复时，输出必须恢复正确输入顺序或明确拒绝，不能错配记忆与向量；由任务 2 的 Provider 测试覆盖。
- 同一进程已有无关模型环境变量时，`local-fake` 仍不能构造外部客户端；由任务 1 和任务 4 的档位测试覆盖。
- 图片后缀缺失或伪造时必须按解码内容识别 JPEG/PNG/WebP，不得依赖文件名；由任务 3 的图片测试覆盖。
- 正式 Add 在智能体成功后 Embedding 失败时不能残留账本、数据库记录或暂存图片；由任务 5 的 HTTP 集成测试覆盖。
- 已提交 request_id 的回放不得重复调用 LLM 或 Embedding，即使正式 Provider 当前不可用；由任务 5 的回放测试覆盖。

---

### Task 1: 运行档位与安全配置

**Files:**
- Modify: `src/masm/config.py`
- Create: `tests/unit/test_runtime_settings.py`

**Interfaces:**
- Produces: `RuntimeProfile(StrEnum)`，值为 `local-fake`、`official-baseline`、`official-masm`。
- Produces: `Settings.validate_runtime() -> None`，正式档位完整校验模型、URL、密钥、维度、超时和尝试次数。
- Produces: `Settings.is_official -> bool`。

- [ ] **Step 1: 写失败测试**

  新增以下测试：
  - `test_local_fake_never_requires_model_credentials`
  - `test_official_profiles_require_both_provider_credentials`
  - `test_official_profiles_reject_wrong_models_and_non_https_urls`
  - `test_model_limits_require_positive_dimensions_timeout_and_one_or_two_attempts`
  - `test_settings_repr_redacts_provider_credentials`
  - `test_from_env_reads_runtime_provider_configuration`

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_runtime_settings.py -q`

  Expected: FAIL，因为 `RuntimeProfile` 和正式模型配置字段尚不存在。

- [ ] **Step 3: 实现最小配置模型**

  在 `config.py` 定义枚举、字段、`is_official` 和 `validate_runtime()`；密钥字段使用 `dataclasses.field(repr=False)`。`from_env()` 读取 `MASM_RUNTIME_PROFILE`、`MASM_LLM_*`、`MASM_EMBEDDING_*` 和 `MASM_MODEL_*`，但不读取或打印密钥值以外的替代来源。

- [ ] **Step 4: 验证 GREEN 与全量回归**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_runtime_settings.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add src/masm/config.py tests/unit/test_runtime_settings.py
  git commit -m "feat: add competition runtime profiles"
  ```

### Task 2: OpenAI-compatible 文本 Embedding Provider

**Files:**
- Create: `src/masm/providers/errors.py`
- Create: `src/masm/providers/openai_embeddings.py`
- Modify: `src/masm/providers/llm.py`
- Create: `tests/unit/providers/test_openai_embeddings.py`
- Modify: `tests/unit/agents/test_llm_provider.py`

**Interfaces:**
- Produces: `ProviderError`、`ProviderUnavailableError`、`ProviderResponseError`。
- Produces: `OpenAICompatibleEmbeddingProvider(EmbeddingProvider)`，构造参数为 `model`、`model_version`、`dimensions`、`base_url`、`api_key`、可选 `httpx.Client`、`timeout_seconds`、`max_attempts`。
- Produces: `embed_texts(texts: Sequence[str]) -> list[list[float]]`；`embed_images()` 明确拒绝直接图片调用。
- Changes: `ModelUnavailableError` 与 `StructuredOutputError` 继承统一 Provider 异常层次，但保持现有调用方兼容。

- [ ] **Step 1: 写失败测试**

  使用 `httpx.MockTransport` 覆盖：
  - `test_embedding_request_uses_fixed_model_dimensions_and_bearer_token`
  - `test_embedding_response_is_restored_by_index`
  - `test_embedding_rejects_missing_duplicate_non_finite_and_wrong_dimensions`
  - `test_embedding_retries_transport_failure_at_most_once`
  - `test_embedding_error_never_contains_secret_or_response_body`
  - `test_direct_image_embedding_is_rejected`
  - 更新 LLM 测试，断言现有异常属于统一 Provider 错误且错误文本不含响应正文。

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/unit/providers/test_openai_embeddings.py tests/unit/agents/test_llm_provider.py -q`

  Expected: FAIL，因为 Provider 类与统一错误尚不存在。

- [ ] **Step 3: 实现 Provider 与错误层次**

  Provider 调用 `POST {base_url}/embeddings`；按 `index` 恢复顺序；验证条目数量、唯一 index、有限浮点数和固定维度。请求记录只保留模型、批量大小、延迟、尝试次数与成功状态。

- [ ] **Step 4: 验证 GREEN 与全量回归**

  Run: `.venv\Scripts\python -m pytest tests/unit/providers/test_openai_embeddings.py tests/unit/agents/test_llm_provider.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add src/masm/providers tests/unit/providers/test_openai_embeddings.py tests/unit/agents/test_llm_provider.py
  git commit -m "feat: add official text embedding provider"
  ```

### Task 3: 图片文本化与统一向量空间

**Files:**
- Create: `src/masm/providers/multimodal_embeddings.py`
- Create: `tests/unit/providers/test_multimodal_embeddings.py`

**Interfaces:**
- Produces: `GroundedMultimodalEmbeddingProvider(EmbeddingProvider)`，构造参数为 `text_embeddings: OpenAICompatibleEmbeddingProvider` 与 `perception: PerceptionAgent`。
- Produces: `canonical_perception_text(result: PerceptionResult) -> str`，稳定拼接可观察描述、OCR、实体和关键词。
- Consumes: Task 2 的 `OpenAICompatibleEmbeddingProvider.embed_texts()` 与统一 Provider 异常。

- [ ] **Step 1: 写失败测试**

  新增：
  - `test_texts_delegate_to_text_embedding_provider`
  - `test_png_jpeg_and_webp_are_detected_from_decoded_bytes`
  - `test_images_are_sent_as_ordered_data_uris_then_embedded_as_canonical_text`
  - `test_text_and_image_vectors_share_model_version_and_dimensions`
  - `test_invalid_or_unsupported_image_bytes_are_rejected_without_network_call`
  - `test_image_perception_failure_never_falls_back_to_fake_vectors`

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/unit/providers/test_multimodal_embeddings.py -q`

  Expected: FAIL，因为组合 Provider 尚不存在。

- [ ] **Step 3: 实现组合 Provider**

  使用 Pillow 从字节识别实际格式并构造 Data URI；每张图片调用受控的 Perception Agent；规范化结果后批量调用 Task 2 Provider。模型名、版本和维度全部代理到 `text_embeddings`，不创建独立图片向量空间。

- [ ] **Step 4: 验证 GREEN 与全量回归**

  Run: `.venv\Scripts\python -m pytest tests/unit/providers/test_multimodal_embeddings.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add src/masm/providers/multimodal_embeddings.py tests/unit/providers/test_multimodal_embeddings.py
  git commit -m "feat: embed images through grounded descriptions"
  ```

### Task 4: Runtime Factory 与真实 MASM 装配

**Files:**
- Create: `src/masm/runtime.py`
- Modify: `src/masm/api/app.py`
- Create: `tests/unit/test_runtime_factory.py`
- Create: `tests/integration/test_runtime_profiles.py`

**Interfaces:**
- Produces: `RuntimeComponents`，字段为 `profile`、`embeddings`、`llm`、`retriever`、`add_pipeline`、`query_analyzer`、`relation_expander`、`reranker`。
- Produces: `build_runtime(settings, repository, *, embeddings=None, llm=None, channel_weights=None) -> RuntimeComponents`。
- Consumes: Tasks 1–3 的档位、Provider 和模型配置。
- Changes: `create_app()` 保留现有依赖注入能力，并把 `RuntimeComponents` 注入 AddService/SearchService。

- [ ] **Step 1: 写失败测试**

  新增：
  - `test_local_fake_builds_no_llm_or_advanced_pipeline`
  - `test_local_fake_ignores_unrelated_model_environment`
  - `test_official_baseline_builds_real_embeddings_without_add_pipeline`
  - `test_official_masm_builds_three_agents_and_all_search_components`
  - `test_official_masm_http_add_invokes_each_agent_once`
  - `test_official_masm_search_returns_evidence_not_generated_answer`
  - `test_app_state_exposes_only_nonsensitive_runtime_metadata`

  正式档位测试注入脚本化 LLM 和 Embedding，不访问公网。

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_runtime_factory.py tests/integration/test_runtime_profiles.py -q`

  Expected: FAIL，因为 Runtime Factory 和应用装配尚不存在。

- [ ] **Step 3: 实现 Runtime Factory 并调整应用工厂顺序**

  按 Repository → Provider → BaselineRetriever → Add/Search 组件 → Service 顺序装配。`official-masm` 注入三智能体、`QueryAnalyzer`、`RelationExpander` 和使用 `LexicalReranker` 的 `EvidenceReranker`；`official-baseline` 不注入治理组件。应用状态只公开 profile、模型名和 Prompt 版本等非敏感审计元数据，不增加公共 HTTP 路由。

- [ ] **Step 4: 验证 GREEN 与全量回归**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_runtime_factory.py tests/integration/test_runtime_profiles.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add src/masm/runtime.py src/masm/api/app.py tests/unit/test_runtime_factory.py tests/integration/test_runtime_profiles.py
  git commit -m "feat: wire competition runtime profiles"
  ```

### Task 5: Provider 故障语义、事务清理与幂等回放

**Files:**
- Modify: `src/masm/api/routes.py`
- Modify: `src/masm/services/add_service.py`（仅在测试暴露真实缺口时修改）
- Create: `tests/contract/test_provider_failures.py`
- Modify: `tests/integration/test_add_idempotency.py`

**Interfaces:**
- Consumes: Task 2 的 `ProviderError`。
- Changes: Add/Search 捕获 `ProviderError` 并返回无敏感信息的 HTTP 503。
- Preserves: 已提交请求在任何模型调用前回放；失败请求不留下 PROCESSING、Memory 或暂存对象。

- [ ] **Step 1: 写失败测试**

  新增：
  - `test_add_provider_failure_returns_503_without_sensitive_detail`
  - `test_search_provider_failure_returns_503_without_sensitive_detail`
  - `test_add_provider_failure_leaves_no_ledger_memory_or_staged_asset`
  - `test_committed_replay_skips_llm_and_embedding_when_providers_are_down`

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/contract/test_provider_failures.py tests/integration/test_add_idempotency.py -q`

  Expected: 新增 503 测试 FAIL；既有幂等测试保持通过。

- [ ] **Step 3: 实现最小错误映射并修补真实清理缺口**

  路由只返回稳定的“模型依赖不可用”错误，不传递 Provider 异常正文。如果失败测试证明 AddService 现有 claim-before/after 边界存在清理缺口，再以最小改动修复；不得重写既有生命周期锁。

- [ ] **Step 4: 验证 GREEN 与全量回归**

  Run: `.venv\Scripts\python -m pytest tests/contract/test_provider_failures.py tests/integration/test_add_idempotency.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add src/masm/api/routes.py src/masm/services/add_service.py tests/contract/test_provider_failures.py tests/integration/test_add_idempotency.py
  git commit -m "fix: make model failures safe and retryable"
  ```

### Task 6: 部署、实验配置与参赛文档同步

**Files:**
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `deployments/README.md`
- Modify: `experiments/README.md`
- Modify: `experiments/configs/b0.yaml`
- Modify: `experiments/configs/masm.yaml`
- Modify: `docs/competition/submission-checklist.md`
- Create: `tests/unit/test_competition_profile_docs.py`

**Interfaces:**
- Consumes: Task 1 的环境变量名和 Task 4 的运行档位。
- Produces: B0 → `official-baseline`、MASM → `official-masm` 的可审计映射；生产 Compose 可通过环境变量显式选择正式档位。

- [ ] **Step 1: 写失败的配置一致性测试**

  `test_competition_profile_docs.py` 断言：
  - `.env.example` 和 Compose 包含全部正式配置名，但不含可用密钥。
  - B0/MASM 的 `deployment_profile` 与运行档位一一对应。
  - README 明确 `local-fake` 禁止正式提交，并且不再声称默认入口已启用完整 MASM。

- [ ] **Step 2: 验证 RED**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_competition_profile_docs.py -q`

  Expected: FAIL，因为文档与配置仍描述旧运行方式。

- [ ] **Step 3: 更新配置模板与中文文档**

  只写无效占位值；说明两套 Provider 密钥、503 语义、真实模型 Smoke、模型规则待组委会确认项，以及正式实验必须记录模型、Prompt、成本和 commit。保留 `record-before-run` 作为真实实验前的阻断标记，直到实际调用完成并有证据可记录。

- [ ] **Step 4: 验证 GREEN、静态检查与全量测试**

  Run: `.venv\Scripts\python -m pytest tests/unit/test_competition_profile_docs.py -q`

  Expected: PASS。

  Run: `.venv\Scripts\ruff check .`

  Expected: PASS，无诊断。

  Run: `.venv\Scripts\mypy src`

  Expected: PASS，无诊断。

  Run: `.venv\Scripts\python -m pytest -q`

  Expected: 全部 PASS。

  Run: `docker compose config`

  Expected: exit 0，且渲染配置不包含仓库外真实密钥。

- [ ] **Step 5: 提交**

  ```bash
  git add .env.example docker-compose.yml README.md deployments/README.md experiments/README.md experiments/configs/b0.yaml experiments/configs/masm.yaml docs/competition/submission-checklist.md tests/unit/test_competition_profile_docs.py
  git commit -m "docs: prepare official competition runtime"
  ```

## 最终验证与审查

完成全部任务后：

1. 运行完整 `pytest -q`、`ruff check .`、`mypy src` 和 `docker compose config`。
2. 用计划执行技能生成 whole-branch review package，并由一个新审查上下文检查规格、计划、差异和 ledger 中的所有裁决。
3. Critical/Important 问题必须按 TDD 修复；Minor 记录为延后项。
4. 真实模型调用、真实费用和官方 Smoke 明确标记为“等待用户提供密钥和公网资源”，不得用 Mock 结果替代。
