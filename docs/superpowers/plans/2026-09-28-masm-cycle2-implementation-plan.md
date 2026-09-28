# MASM Cycle 2 实施计划

> **供智能体执行：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施本计划。所有步骤使用复选框（`- [ ]`）追踪。DeepSeek Harness 应采用等价的逐任务执行、验证和提交机制。

**目标：** 构建一个能够参加 Agent Memory Challenge Cycle 2 多模态开源方法组的 MASM 系统，先交付可提交的多模态记忆基线，再增加三智能体结构化写入、关系扩展和冲突感知重排。

**架构：** 系统采用 FastAPI 模块化单体，使用 PostgreSQL + pgvector 保存结构化记忆和向量，使用兼容 S3 的对象存储保存图片。所有模型通过 Provider 接口调用；Add 由确定性编排器控制，Search 使用多路召回、RRF 融合、一跳关系扩展和证据重排。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16、pgvector、HTTPX、兼容 S3 的对象存储、Docker Compose、Pytest、Ruff、Mypy。

**设计规范：** `docs/superpowers/specs/2026-09-28-agent-memory-challenge-design.md`

## 全局约束

- 只实现文本和图片，不实现音频、视频、前端或通用聊天功能。
- 公共接口只有 `GET /health`、`POST /add` 和 `POST /search`。
- Search 只返回记忆证据，不生成最终答案。
- Add 返回成功时，记忆必须已经持久化并可立即检索。
- `request_id` 必须保证 Add 幂等。
- 所有存储和检索接口都必须强制接收 `user_id`，不能提供无用户作用域的重载。
- 单图解码后最大 10 MiB；单次 Add 图片总量最大 30 MiB；Search 响应总量最大 30 MiB。
- Search 必须支持 `top_k=100`。
- 原始证据不可覆盖；重复、替代和冲突通过关系与状态管理。
- 模型最多自动重试或修复一次，之后执行文档化降级。
- 日志不得包含原始文本、图片、Base64、密钥或含评测内容的 Prompt。
- 密钥只通过环境变量注入，仓库只提交 `.env.example`。
- 项目文档默认使用中文；代码标识、官方接口、模型名、专有名词或中文表达会造成歧义时可以使用英文。
- 每个任务必须先写失败测试，再实现最小代码，再运行完整相关测试，最后独立提交。
- DeepSeek Harness 不得在单个任务中修改该任务“禁止修改”的边界。

## 重点审查项

以下五类问题最容易通过普通单元测试但在比赛中失败，必须由指定任务的测试固定行为：

1. **跨用户泄漏：** 两个用户拥有近乎相同的文本、图片、实体和关系时，所有检索路径仍只能返回目标用户数据；由任务 4、7 和 8 的隔离测试覆盖。
2. **重试导致重复写入：** Add 已提交但客户端未收到响应，再次发送相同 `request_id` 时必须返回原结果；由任务 3 的重启幂等测试覆盖。
3. **畸形或超限图片：** 合法 Data URL、非法 Base64、错误媒体类型、单图超限和总量超限必须产生确定状态码；由任务 3 的媒体契约测试覆盖。
4. **模型超时或无效 JSON：** Agent Provider 超时、返回非 JSON 或不符合 Schema 时只能重试一次，随后保存可检索基线记忆；由任务 6 的降级测试覆盖。
5. **大规模响应裁剪：** `top_k=100` 且候选含多张图片时，响应不能超过 30 MiB，不能切断单条证据，并必须保持排序前缀；由任务 8 的打包测试覆盖。

---

## 文件结构锁定

实现过程中使用以下职责边界：

- `src/masm/api/`：只处理 HTTP、认证、请求映射和状态码。
- `src/masm/schemas/`：官方契约及智能体结构化输出模型。
- `src/masm/services/`：Add/Search 应用服务和事务编排。
- `src/masm/agents/`：三个智能体及其 Prompt，不访问数据库。
- `src/masm/retrieval/`：召回、融合、扩展、重排和打包算法。
- `src/masm/storage/`：数据库模型、Repository、对象存储和迁移。
- `src/masm/providers/`：模型、Embedding 和 Reranker 外部适配器。
- `src/masm/observability/`：脱敏日志和指标。
- `tests/contract/`：官方 HTTP 契约。
- `tests/isolation/`：跨用户泄漏阻断测试。
- `experiments/`：基准适配器、实验配置和结果汇总，不被线上应用导入。

## 任务 1：项目基础、配置和官方 API Schema

**文件：**

- 创建：`pyproject.toml`
- 创建：`.env.example`
- 创建：`README.md`
- 创建：`src/masm/__init__.py`
- 创建：`src/masm/config.py`
- 创建：`src/masm/schemas/content.py`
- 创建：`src/masm/schemas/api.py`
- 创建：`src/masm/api/app.py`
- 创建：`src/masm/api/routes.py`
- 创建：`tests/contract/test_health.py`
- 创建：`tests/contract/test_api_schemas.py`

**接口：**

- 产出：`Settings.from_env() -> Settings`
- 产出：`create_app(settings: Settings) -> FastAPI`
- 产出：`TextPart`、`ImageURLPart`、`Message`、`AddRequest`、`AddResponse`、`SearchRequest`、`MemoryEvidence`、`SearchResponse`
- 约束：`Message.content` 和 `SearchRequest.query` 接受纯文本或保持顺序的内容数组。
- 禁止修改：设计规范、官方字段名、官方响应外形。

- [ ] **步骤 1：编写失败的 Health 与 Schema 契约测试**

  测试名称必须包括 `test_health_is_public_and_returns_2xx`、`test_add_request_preserves_mixed_content_order`、`test_search_accepts_top_k_100`、`test_remote_image_url_is_rejected`。断言 Health 无需认证；混合内容顺序不变；`top_k=101` 校验失败；图片只接受 `data:image/...;base64,...`。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/contract/test_health.py tests/contract/test_api_schemas.py -v`  
  预期：因 `masm` 包或 Schema 不存在而失败。

- [ ] **步骤 3：实现最小项目、配置、Schema 和 Health**

  `Settings` 固定包含 `database_url: str`、`api_keys: tuple[str, ...]`、`max_image_bytes: int = 10485760`、`max_add_image_bytes: int = 31457280`、`max_search_response_bytes: int = 31457280`、`max_top_k: int = 100`。Health 只返回不含依赖机密的状态对象。

- [ ] **步骤 4：运行契约测试、静态检查和类型检查**

  运行：`pytest tests/contract/test_health.py tests/contract/test_api_schemas.py -v && ruff check . && mypy src`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 1**

  ```bash
  git add pyproject.toml .env.example README.md src tests/contract
  git commit -m "feat: scaffold MASM API contracts"
  ```

## 任务 2：数据库模型、迁移和强制用户作用域

**文件：**

- 创建：`alembic.ini`
- 创建：`alembic/env.py`
- 创建：`alembic/versions/0001_initial_memory_schema.py`
- 创建：`src/masm/storage/db.py`
- 创建：`src/masm/storage/models.py`
- 创建：`src/masm/storage/repositories.py`
- 创建：`src/masm/storage/types.py`
- 创建：`tests/unit/storage/test_repository_scope.py`
- 创建：`tests/integration/storage/test_migrations.py`

**接口：**

- 产出：`Database.create(database_url: str) -> Database`
- 产出：`MemoryRepository.add_bundle(user_id: str, bundle: MemoryBundle) -> AddCommit`
- 产出：`MemoryRepository.get_by_request(user_id: str, request_id: str) -> AddCommit | None`
- 产出：`MemoryRepository.lexical_candidates(user_id: str, query: str, limit: int) -> list[MemoryCandidate]`
- 产出：`MemoryRepository.vector_candidates(user_id: str, vector: Sequence[float], limit: int) -> list[MemoryCandidate]`
- 产出：`MemoryRepository.related(user_id: str, memory_ids: Sequence[UUID], limit: int) -> list[MemoryCandidate]`
- 禁止修改：任务 1 的公共 Schema 和路由。

- [ ] **步骤 1：编写迁移和用户作用域失败测试**

  测试必须断言全部必需表存在；Repository 的所有读取签名都要求 `user_id`；用户 A 的全文和向量候选不能包含用户 B 的记录。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/storage tests/integration/storage -v`  
  预期：因存储模块或迁移不存在而失败。

- [ ] **步骤 3：实现数据库模型、迁移和 Repository**

  建立设计规范中的 11 类表。原始内容和派生内容分表；向量记录包含模型名、版本和维度；关系边的两端都必须能够验证属于同一用户。

- [ ] **步骤 4：运行迁移、存储测试和静态检查**

  运行：`alembic upgrade head && pytest tests/unit/storage tests/integration/storage -v && ruff check src tests`  
  预期：迁移成功且测试通过。

- [ ] **步骤 5：提交任务 2**

  ```bash
  git add alembic.ini alembic src/masm/storage tests/unit/storage tests/integration/storage
  git commit -m "feat: add scoped memory persistence"
  ```

## 任务 3：认证、媒体校验、对象存储和幂等 Add 基线

**文件：**

- 创建：`src/masm/api/auth.py`
- 创建：`src/masm/api/limits.py`
- 创建：`src/masm/services/add_service.py`
- 创建：`src/masm/storage/assets.py`
- 创建：`src/masm/schemas/internal.py`
- 修改：`src/masm/api/routes.py`
- 修改：`src/masm/api/app.py`
- 创建：`tests/contract/test_auth.py`
- 创建：`tests/contract/test_rate_limits.py`
- 创建：`tests/contract/test_add_media_limits.py`
- 创建：`tests/integration/test_add_idempotency.py`
- 创建：`tests/integration/test_add_durability.py`

**接口：**

- 消费：任务 1 的 `AddRequest` 和任务 2 的 `MemoryRepository`。
- 产出：`decode_image_data_url(value: str, max_bytes: int) -> DecodedImage`
- 产出：`AssetStore.put(user_id: str, request_id: str, image: DecodedImage) -> AssetRef`
- 产出：`AddService.add(request: AddRequest) -> AddResponse`
- 产出：`RequestLimiter.acquire(api_key_id: str) -> AsyncContextManager[None]`
- 约束：Add 基线保存原始文本、图片、基础可检索文本和对象引用，不调用智能体。
- 禁止修改：数据库表含义、Add 官方响应结构。

- [ ] **步骤 1：编写认证、媒体和幂等失败测试**

  覆盖 Bearer、Token 和 `X-Api-Key`；非法 Base64 返回 400；不支持的媒体类型返回 422；单图超过 10 MiB 或总图片超过 30 MiB 返回 413；相同 `request_id` 在应用重启后仍只有一组记录和资源；达到速率或并发限制时返回带 `Retry-After` 的 429。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/contract/test_auth.py tests/contract/test_rate_limits.py tests/contract/test_add_media_limits.py tests/integration/test_add_idempotency.py tests/integration/test_add_durability.py -v`  
  预期：因认证、媒体或 Add 服务不存在而失败。

- [ ] **步骤 3：实现认证、图片解码、对象存储与 Add 事务**

  使用内容哈希生成对象键；先保存暂存资源，再在数据库事务成功后标记提交；事务失败时清理暂存对象。幂等账本状态为 `PROCESSING`、`COMMITTED` 或 `FAILED`，只有 `COMMITTED` 可复用成功结果。

- [ ] **步骤 4：运行 Add 相关测试**

  运行：`pytest tests/contract/test_auth.py tests/contract/test_rate_limits.py tests/contract/test_add_media_limits.py tests/integration/test_add_idempotency.py tests/integration/test_add_durability.py -v`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 3**

  ```bash
  git add src/masm/api src/masm/services src/masm/storage/assets.py src/masm/schemas/internal.py tests
  git commit -m "feat: add durable idempotent memory ingestion"
  ```

## 任务 4：Embedding Provider 与可提交的混合检索基线

**文件：**

- 创建：`src/masm/providers/embeddings.py`
- 创建：`src/masm/providers/fakes.py`
- 创建：`src/masm/retrieval/rrf.py`
- 创建：`src/masm/retrieval/baseline.py`
- 创建：`src/masm/services/search_service.py`
- 修改：`src/masm/services/add_service.py`
- 修改：`src/masm/api/routes.py`
- 创建：`tests/unit/retrieval/test_rrf.py`
- 创建：`tests/integration/test_add_search_baseline.py`
- 创建：`tests/isolation/test_baseline_user_isolation.py`

**接口：**

- 产出：`EmbeddingProvider.embed_texts(texts: Sequence[str]) -> list[list[float]]`
- 产出：`EmbeddingProvider.embed_images(images: Sequence[bytes]) -> list[list[float]]`
- 产出：`reciprocal_rank_fusion(rankings: Sequence[RankedChannel], weights: Mapping[str, float], k: int = 60) -> list[FusedCandidate]`
- 产出：`BaselineRetriever.retrieve(user_id: str, query: ParsedQuery, limit: int) -> list[MemoryCandidate]`
- 产出：`SearchService.search(request: SearchRequest) -> SearchResponse`
- 禁止修改：Add 幂等行为和公共响应 Schema。

- [ ] **步骤 1：编写 RRF、端到端检索和隔离失败测试**

  使用确定性 Fake Embedding Provider。测试 Add 后立即 Search；文本能检索图片的基础描述；图片能检索相似图片；`top_k=100` 被接受；两个用户拥有相同向量时仍完全隔离。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/retrieval/test_rrf.py tests/integration/test_add_search_baseline.py tests/isolation/test_baseline_user_isolation.py -v`  
  预期：因 Provider、Retriever 或 SearchService 不存在而失败。

- [ ] **步骤 3：实现 Embedding 接口、RRF 与基线 Search**

  Add 同步生成文本和图片向量；Search 并行执行全文、文本向量、图片向量和元数据召回，再使用配置权重融合。测试和本地开发只使用 Fake Provider，不访问付费 API。

- [ ] **步骤 4：运行基线完整测试**

  运行：`pytest tests/contract tests/unit/retrieval tests/integration/test_add_search_baseline.py tests/isolation/test_baseline_user_isolation.py -v`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 4，并标记首个可回滚基线**

  ```bash
  git add src/masm/providers src/masm/retrieval src/masm/services tests
  git commit -m "feat: add multimodal retrieval baseline"
  git tag baseline-v0.1
  ```

## 任务 5：结构化智能体 Schema 与模型 Provider

**文件：**

- 创建：`src/masm/schemas/agents.py`
- 创建：`src/masm/providers/llm.py`
- 创建：`src/masm/agents/perception.py`
- 创建：`src/masm/agents/temporal.py`
- 创建：`src/masm/agents/prompts/perception_v1.txt`
- 创建：`src/masm/agents/prompts/temporal_v1.txt`
- 创建：`tests/unit/agents/test_agent_schemas.py`
- 创建：`tests/unit/agents/test_perception_agent.py`
- 创建：`tests/unit/agents/test_temporal_agent.py`

**接口：**

- 产出：`StructuredLLM.complete_json(request: ModelRequest, output_type: type[T]) -> T`
- 产出：`PerceptionAgent.extract(content: Sequence[ContentPart]) -> PerceptionResult`
- 产出：`TemporalRelationAgent.analyze(perception: PerceptionResult, history: Sequence[MemoryCandidate]) -> TemporalRelationResult`
- 约束：两个智能体只返回 Schema 对象，不接收 Repository 或数据库会话。
- 禁止修改：公共 API、Repository 接口、基线 Retriever。

- [ ] **步骤 1：编写 Schema、忠实性约束和 Fake Provider 测试**

  测试无效实体、越界置信度和无来源关系会被拒绝；Prompt 明确禁止推断不可观察事实；历史候选数量受配置上限约束。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/agents/test_agent_schemas.py tests/unit/agents/test_perception_agent.py tests/unit/agents/test_temporal_agent.py -v`  
  预期：因 Agent Schema 或实现不存在而失败。

- [ ] **步骤 3：实现结构化 Provider 与前两个智能体**

  Provider 必须支持超时、一次重试、JSON Schema 校验、模型与 Prompt 版本记录。Prompt 文件不得包含基准题目或答案示例。

- [ ] **步骤 4：运行智能体单元测试**

  运行：`pytest tests/unit/agents -v && ruff check src/masm/agents src/masm/providers src/masm/schemas`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 5**

  ```bash
  git add src/masm/agents src/masm/providers/llm.py src/masm/schemas/agents.py tests/unit/agents
  git commit -m "feat: add structured perception and temporal agents"
  ```

## 任务 6：记忆管理智能体、编排器和可靠降级

**文件：**

- 创建：`src/masm/agents/curator.py`
- 创建：`src/masm/agents/prompts/curator_v1.txt`
- 创建：`src/masm/orchestration/add_pipeline.py`
- 创建：`src/masm/orchestration/action_validator.py`
- 修改：`src/masm/services/add_service.py`
- 修改：`src/masm/storage/repositories.py`
- 创建：`tests/unit/agents/test_curator_agent.py`
- 创建：`tests/unit/orchestration/test_action_validator.py`
- 创建：`tests/integration/test_agent_add_pipeline.py`
- 创建：`tests/integration/test_agent_degradation.py`

**接口：**

- 产出：`MemoryCuratorAgent.propose(new_memory: MemoryDraft, history: Sequence[MemoryCandidate]) -> CuratorDecision`
- 产出：`validate_actions(user_id: str, decision: CuratorDecision, candidates: Sequence[MemoryCandidate]) -> ValidatedActions`
- 产出：`AddPipeline.process(request: AddRequest) -> MemoryBundle`
- 约束：允许动作只有 `CREATE`、`LINK`、`MERGE`、`SUPERSEDE`、`CONFLICT`。
- 禁止修改：原始证据不可变规则、任务 4 的基线开关和标签。

- [ ] **步骤 1：编写动作校验、端到端 Agent Add 和降级失败测试**

  测试跨用户关系、未知动作、循环替代和覆盖原始证据都被拒绝；Provider 超时、非 JSON、Schema 错误均只重试一次，然后保存可 Search 的基线记忆。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/agents/test_curator_agent.py tests/unit/orchestration tests/integration/test_agent_add_pipeline.py tests/integration/test_agent_degradation.py -v`  
  预期：因 Curator 或编排器不存在而失败。

- [ ] **步骤 3：实现 Curator、动作校验和 Add 编排器**

  编排器使用显式状态机，不允许智能体自行递归；历史候选有固定上限；任何数据库变更都通过 `ValidatedActions` 执行。

- [ ] **步骤 4：运行 Add 流水线与全部基线回归测试**

  运行：`pytest tests/unit/agents tests/unit/orchestration tests/integration tests/contract/test_add_media_limits.py -v`  
  预期：全部通过，基线模式仍可运行。

- [ ] **步骤 5：提交任务 6**

  ```bash
  git add src/masm/agents src/masm/orchestration src/masm/services/add_service.py src/masm/storage/repositories.py tests
  git commit -m "feat: orchestrate structured memory agents"
  ```

## 任务 7：查询分析、一跳关系扩展和冲突感知重排

**文件：**

- 创建：`src/masm/retrieval/query_analyzer.py`
- 创建：`src/masm/retrieval/relation_expander.py`
- 创建：`src/masm/retrieval/reranker.py`
- 创建：`src/masm/providers/reranker.py`
- 修改：`src/masm/services/search_service.py`
- 创建：`tests/unit/retrieval/test_query_analyzer.py`
- 创建：`tests/unit/retrieval/test_relation_expander.py`
- 创建：`tests/unit/retrieval/test_conflict_reranking.py`
- 创建：`tests/isolation/test_relation_user_isolation.py`

**接口：**

- 产出：`QueryAnalyzer.parse(query: QueryContent) -> ParsedQuery`
- 产出：`RelationExpander.expand(user_id: str, seeds: Sequence[MemoryCandidate], limit: int) -> list[MemoryCandidate]`
- 产出：`EvidenceReranker.rank(query: ParsedQuery, candidates: Sequence[MemoryCandidate]) -> list[RankedEvidence]`
- 约束：查询最多产生三个子查询；关系最多扩展一跳；冲突双方可以成组返回。
- 禁止修改：官方 Search Schema、Repository 的强制用户作用域。

- [ ] **步骤 1：编写查询上限、关系深度、冲突覆盖和隔离失败测试**

  测试复杂查询最多三个子查询；二跳节点不会进入结果；冲突组命中一侧时可补全另一侧；跨用户关系边即使数据库中异常存在也必须被过滤。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/retrieval/test_query_analyzer.py tests/unit/retrieval/test_relation_expander.py tests/unit/retrieval/test_conflict_reranking.py tests/isolation/test_relation_user_isolation.py -v`  
  预期：因增强检索模块不存在而失败。

- [ ] **步骤 3：实现查询分析、扩展和重排**

  简单查询优先使用规则，复杂查询才调用 LLM；Reranker 只产生相关性和排序元数据，不生成答案文本。

- [ ] **步骤 4：运行增强 Search 与全部隔离测试**

  运行：`pytest tests/unit/retrieval tests/isolation tests/integration/test_add_search_baseline.py -v`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 7**

  ```bash
  git add src/masm/retrieval src/masm/providers/reranker.py src/masm/services/search_service.py tests
  git commit -m "feat: add relation-aware evidence ranking"
  ```

## 任务 8：响应打包、日志脱敏和数据删除

**文件：**

- 创建：`src/masm/retrieval/response_packer.py`
- 创建：`src/masm/observability/logging.py`
- 创建：`src/masm/observability/metrics.py`
- 创建：`src/masm/services/deletion_service.py`
- 创建：`scripts/delete_evaluation_run.py`
- 修改：`src/masm/services/search_service.py`
- 创建：`tests/unit/retrieval/test_response_packer.py`
- 创建：`tests/unit/observability/test_log_redaction.py`
- 创建：`tests/integration/test_delete_evaluation_run.py`
- 创建：`tests/isolation/test_full_path_user_isolation.py`

**接口：**

- 产出：`ResponsePacker.pack(evidence: Sequence[RankedEvidence], top_k: int, max_bytes: int) -> SearchResponse`
- 产出：`configure_logging(settings: Settings) -> None`
- 产出：`DeletionService.delete_run(run_id: str) -> DeletionReport`
- 约束：响应裁剪只能保留完整证据，且保持排序前缀；删除同时覆盖数据库与对象存储。
- 禁止修改：相关性排序结果和官方 Search 响应字段。

- [ ] **步骤 1：编写 30 MiB 裁剪、日志脱敏、删除和全路径隔离失败测试**

  测试 `top_k=100` 与多图片候选不会超过 30 MiB；任何一条证据不被截断；日志不含原文、Base64、API Key 或 Prompt；删除指定运行不影响其他用户和运行。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/retrieval/test_response_packer.py tests/unit/observability/test_log_redaction.py tests/integration/test_delete_evaluation_run.py tests/isolation/test_full_path_user_isolation.py -v`  
  预期：因打包、日志或删除服务不存在而失败。

- [ ] **步骤 3：实现打包、脱敏指标和删除服务**

  响应大小使用实际序列化字节数计算；日志只允许请求标识、匿名用户标识、延迟、数量、模型版本、token 与成本元数据。

- [ ] **步骤 4：运行安全、隔离和回归测试**

  运行：`pytest tests/unit tests/integration tests/contract tests/isolation -v`  
  预期：全部通过。

- [ ] **步骤 5：提交任务 8**

  ```bash
  git add src/masm/retrieval src/masm/observability src/masm/services scripts tests
  git commit -m "feat: enforce response and privacy boundaries"
  ```

## 任务 9：容器部署、依赖健康检查和 Smoke 验证

**文件：**

- 创建：`Dockerfile`
- 创建：`docker-compose.yml`
- 创建：`deployments/Caddyfile`
- 创建：`deployments/README.md`
- 创建：`THIRD_PARTY_NOTICES.md`
- 创建：`docs/competition/submission-checklist.md`
- 创建：`scripts/smoke_test.py`
- 修改：`src/masm/api/app.py`
- 修改：`README.md`
- 创建：`tests/contract/test_dependency_health.py`
- 创建：`tests/integration/test_restart_durability.py`

**接口：**

- 产出：`GET /health` 的公共浅健康检查。
- 产出：仅供部署环境使用的内部依赖探针，不暴露密钥或连接地址。
- 产出：`python scripts/smoke_test.py --base-url URL --api-key KEY`。
- 禁止修改：业务逻辑、数据模型和 Search 排序。

- [ ] **步骤 1：编写依赖状态和重启持久性失败测试**

  测试公共 Health 在无认证时返回 2xx；内部依赖异常不会泄露连接串；容器重启后此前 Add 的数据仍可 Search。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/contract/test_dependency_health.py tests/integration/test_restart_durability.py -v`  
  预期：缺少部署或持久化验证实现而失败。

- [ ] **步骤 3：实现 Docker Compose、HTTPS 入口和 Smoke 脚本**

  Compose 至少包含 API 和 PostgreSQL + pgvector；图片对象存储使用可配置外部 S3，测试环境可使用兼容本地服务；所有持久数据使用命名卷。`THIRD_PARTY_NOTICES.md` 记录复用项目、论文、许可证和修改内容；提交检查表使用中文记录公开仓库、固定提交、API 地址、认证、容量、超时和版本一致性。

- [ ] **步骤 4：执行本地部署和 Smoke 验证**

  运行：`docker compose up -d --build`，随后运行 `python scripts/smoke_test.py --base-url http://localhost:8000 --api-key test-key`。  
  预期：Health、Add、立即 Search、重复 Add、重启后 Search 全部通过。

- [ ] **步骤 5：提交任务 9，并标记提交候选版本**

  ```bash
  git add Dockerfile docker-compose.yml deployments scripts/smoke_test.py src/masm/api README.md THIRD_PARTY_NOTICES.md docs/competition tests
  git commit -m "feat: add competition deployment and smoke checks"
  git tag submission-rc1
  ```

## 任务 10：公开基准、消融配置和可复现实验报告

**文件：**

- 创建：`experiments/README.md`
- 创建：`experiments/configs/b0.yaml`
- 创建：`experiments/configs/b1.yaml`
- 创建：`experiments/configs/masm.yaml`
- 创建：`experiments/configs/ablations/*.yaml`
- 创建：`experiments/adapters/atm_bench.py`
- 创建：`experiments/adapters/mem_gallery.py`
- 创建：`experiments/run_experiment.py`
- 创建：`experiments/summarize_results.py`
- 创建：`tests/unit/experiments/test_metric_calculation.py`
- 创建：`tests/integration/experiments/test_small_fixture_run.py`
- 修改：`README.md`

**接口：**

- 产出：`run_experiment(config_path: Path, output_dir: Path) -> ExperimentManifest`
- 产出：`summarize_results(manifests: Sequence[ExperimentManifest]) -> ComparisonReport`
- 约束：实验模块只能通过公共 Add/Search 接口评测系统，不得导入线上 Repository 或内部答案。
- 禁止修改：线上代码、官方接口和已冻结候选版本。

- [ ] **步骤 1：编写指标和小型固定样例失败测试**

  固定样例必须验证 Recall@10、Recall@100、MRR、nDCG、延迟、成本、模型调用次数和降级率的计算；同一数据与种子重复运行产生相同清单。

- [ ] **步骤 2：运行测试并确认失败**

  运行：`pytest tests/unit/experiments tests/integration/experiments -v`  
  预期：因实验适配器和指标实现不存在而失败。

- [ ] **步骤 3：实现基准适配器、配置和汇总工具**

  先支持小型可控子集。B0、B1、MASM 与每个消融配置必须记录 Git 提交、模型版本、Prompt 版本、数据集版本、随机种子和成本。

- [ ] **步骤 4：运行固定样例和首轮小规模实验**

  运行：`pytest tests/unit/experiments tests/integration/experiments -v`，再运行 `python experiments/run_experiment.py --config experiments/configs/b0.yaml --output artifacts/experiments/b0-smoke`。  
  预期：测试通过，输出机器可读清单和不含评测原文的汇总结果。

- [ ] **步骤 5：提交任务 10**

  ```bash
  git add experiments tests/unit/experiments tests/integration/experiments README.md
  git commit -m "feat: add reproducible memory benchmarks"
  ```

## 最终验收

- [ ] 运行完整测试：`pytest -v`。
- [ ] 运行静态检查：`ruff check . && mypy src`。
- [ ] 从空数据库执行：`alembic upgrade head`。
- [ ] 构建并启动：`docker compose up -d --build`。
- [ ] 运行 Smoke：Health、Add、立即 Search、重复 Add、重启后 Search。
- [ ] 确认全部隔离测试通过。
- [ ] 确认日志中没有原始文本、Base64、API Key 或 Prompt。
- [ ] 确认 `git status --short` 为空。
- [ ] 将线上镜像、配置和 Git 提交固定为同一发布版本。
- [ ] 保留 `baseline-v0.1` 和最终提交候选的可回滚镜像。

## DeepSeek Harness 执行规则

1. 每次只执行一个任务，不得提前实现后续任务。
2. 开始任务前必须阅读本计划、设计规范以及该任务依赖任务的公开接口。
3. 如果测试要求与官方契约冲突，立即停止并报告，不得自行改契约。
4. 如果需要新增依赖，先报告依赖名称、版本、许可证、用途和替代方案。
5. 每个任务完成后输出：修改文件、测试命令、测试结果、已知限制、提交哈希。
6. 任何失败测试都不得通过删除断言、跳过测试或放宽用户隔离来解决。
7. 所有付费 API 测试默认使用 Fake Provider；只有明确授权的 Smoke 或基准运行可以产生费用。
