# MASM 正式模型与比赛运行链路设计

**日期：** 2026-09-29

**状态：** 已批准，等待实施

**项目根目录：** `E:\Competitions\AgentMemoryChallenge`

**适用目标：** Agent Memory Challenge Cycle 2，多模态赛道，开源方法组

## 1. 目的与成功标准

本轮把现有的可靠工程原型升级为真实参赛候选版本。目标不是扩大系统范围，而是补齐当前最关键的缺口：真实模型 Provider、生产入口中的 MASM Add 流水线、增强 Search 组件、比赛运行配置和可核验的降级行为。

本轮成功标准如下：

1. 本地测试档位仍可完全离线运行，不需要模型密钥。
2. 正式档位只使用当前比赛允许的模型组合：LLM 为 `gpt-4o-mini`，Embedding 为 `text-embedding-v4`；Reranker 保持可配置。
3. 正式档位缺少密钥或模型配置时启动失败，不得静默使用 Fake Provider。
4. 正式 MASM 档位真实执行 Perception、Temporal-Relation、Curator 三个智能体，并启用查询分析、一跳关系扩展和证据重排。
5. 图片和文本最终进入同一个 `text-embedding-v4` 向量空间，避免使用规则未确认的额外图片 Embedding 模型。
6. 模型依赖失败时产生可识别的 503 或文档化降级，不遗留 PROCESSING 账本，也不提交不可检索的成功结果。
7. `/add` 和 `/search` 的官方请求、响应及“只返回证据”边界保持不变。
8. 所有新增行为用 MockTransport/Fake Provider 完成自动化测试；没有真实密钥时不得发起外部网络请求。

## 2. 现状与约束

当前 `create_app()` 默认构造 `DeterministicFakeEmbeddingProvider`，且没有把 `AddPipeline` 注入 `AddService`。三智能体、关系扩展和冲突重排已经实现并分别通过测试，但生产应用工厂没有把它们组成完整运行链路。

正式比赛当前规则要求参赛者自行托管 Add/Search API。官方 FAQ 指定学术/开源方法方向的 Embedding 使用 `text-embedding-v4`，LLM 组件使用 `gpt-4o-mini`，Reranker 不限。本设计把模型名做成受校验的配置项，以便规则更新时只改配置和审计材料，不改业务接口。

本轮不包含公网服务器采购、真实密钥注入、Full 评测、论文实验结果生成或额外模型训练。这些工作依赖外部账号、导师论文和部署资源，将在本轮代码具备真实运行能力后继续。

## 3. 方案比较

### 3.1 方案 A：官方模型组合，图片先结构化再进入文本向量空间（采用）

- `gpt-4o-mini` 负责图片感知、结构化抽取和需要 LLM 的智能体步骤。
- `text-embedding-v4` 负责全部向量；图片先由 `gpt-4o-mini` 生成忠实的结构化描述，再嵌入同一文本空间。
- 基线与完整 MASM 共用 Provider，只通过运行档位决定是否启用智能体治理。

优点是符合当前规则、无需新增本地 GPU、图文处于同一可比较向量空间，且能复用已有 Provider 和智能体实现。缺点是图片处理会增加一次模型调用，Full 评测前必须测量成本与延迟。

### 3.2 方案 B：只使用数据集 caption，忽略原图（不采用）

该方案成本最低，也能兼容文本 Embedding，但会丢失图片中 caption 未覆盖的视觉证据，不符合项目“原图优先”的研究目标，只保留为紧急降级或消融实验。

### 3.3 方案 C：本地 CLIP/SigLIP 图片向量（本轮不采用）

该方案图片相似度能力较强，但当前官方模型约束存在歧义，且会增加模型部署、向量空间融合和 GPU 运维工作。除非组委会明确允许，或改投不受该限制的组别，否则不进入正式参赛档位。

## 4. 运行档位

新增显式 `MASM_RUNTIME_PROFILE`，只允许以下值：

| 档位 | Embedding | Add 智能体 | Search 增强 | 用途 |
| --- | --- | --- | --- | --- |
| `local-fake` | 确定性 Fake | 关闭 | 基线 | 单元测试、离线开发、Docker Smoke |
| `official-baseline` | `text-embedding-v4`，图片经视觉文本化 | 关闭 | 规则分析 + 基础混合召回 | B0、保底提交候选 |
| `official-masm` | 同上 | 三智能体全部启用 | 查询分析 + 一跳扩展 + 重排 | 完整系统、主要提交候选 |

`local-fake` 是开发默认值，但部署文档必须明确：正式提交只能使用 `official-baseline` 或 `official-masm`。Docker Compose 的生产示例显式写出正式档位，不依赖默认值。

## 5. 配置与密钥

`Settings` 新增以下字段：

- `runtime_profile`
- `llm_base_url`
- `llm_api_key`
- `llm_model`，正式档位默认并校验为 `gpt-4o-mini`
- `embedding_base_url`
- `embedding_api_key`
- `embedding_model`，正式档位默认并校验为 `text-embedding-v4`
- `embedding_dimensions`
- `model_timeout_seconds`
- `model_max_attempts`，硬上限继续为 2

LLM 和 Embedding 分别配置 Base URL 与密钥，因为二者可能由不同 OpenAI-compatible 服务提供。密钥只能来自环境变量，不得进入 dataclass repr、日志、错误响应、实验 manifest 或仓库文件。

正式档位启动时统一执行配置校验：模型名不合规、密钥为空、Base URL 不是 HTTPS（测试注入除外）、维度非正数或重试次数越界，都应在服务接收请求前失败。`local-fake` 不读取也不要求外部密钥。

## 6. Provider 设计

### 6.1 文本 Embedding Provider

新增 OpenAI-compatible Embedding Provider：

- 调用 `POST {embedding_base_url}/embeddings`。
- 请求携带固定模型名和文本数组，并在服务支持时携带固定维度。
- 响应按 `index` 恢复输入顺序，拒绝缺项、重复 index、非有限数值和维度不一致。
- 传输失败、非 2xx 或非法响应统一抛出不含原文与密钥的 Provider 异常。
- 调用元数据只记录模型名、批量大小、延迟、尝试次数和成功状态。

### 6.2 图片文本化与同空间嵌入

正式多模态 Provider 的 `embed_images()` 执行：

1. 根据解码后的图片字节识别 JPEG、PNG 或 WebP。
2. 构造内联 Base64 Data URI。
3. 使用 `gpt-4o-mini` 和受版本控制的 Prompt 输出严格 Schema：可观察描述、OCR 文本、实体和关键词。
4. 将结构化结果规范化为稳定文本。
5. 交给 `text-embedding-v4`，因此文本与图片向量的模型名、版本和维度一致。

图片描述不得推断不可观察事实。非法图片在进入 Provider 前仍由现有媒体校验拒绝。图片文本化失败时不得使用 Fake 向量；Add/Search 返回可重试的模型依赖错误。

### 6.3 LLM Provider

复用并加固 `OpenAICompatibleLLM`：

- 正式模型固定为 `gpt-4o-mini`。
- 保持 JSON Schema 输出和 Pydantic 校验。
- 保持“首次调用 + 至多一次重试”。
- 非 2xx、超时和非法结构转换为稳定的 Provider 异常，不在异常文本中附带响应正文。
- 测试使用 `httpx.MockTransport`，不接触真实服务。

## 7. 应用装配

新增独立运行时工厂，避免继续扩大 `api/app.py` 的职责。工厂根据 `runtime_profile` 构造并返回以下依赖：

- Embedding Provider
- 可选 Structured LLM
- 可选 AddPipeline
- QueryAnalyzer
- 可选 RelationExpander
- EvidenceReranker

装配顺序固定为：Repository → Provider → BaselineRetriever → AddPipeline/Search 组件 → AddService/SearchService。

### 7.1 `local-fake`

- 使用 `DeterministicFakeEmbeddingProvider`。
- 不创建外部 LLM。
- `AddService.pipeline=None`。
- Search 保持现有基线行为。

### 7.2 `official-baseline`

- 使用正式多模态 Embedding Provider。
- 不启用治理智能体。
- Search 使用确定性规则查询分析和现有混合召回。
- 不执行关系扩展，避免把不存在的结构化关系当成有效信号。

### 7.3 `official-masm`

- 创建一个共享的 `OpenAICompatibleLLM`。
- 用该实例构造 Perception、Temporal-Relation、Curator。
- 将 `AddPipeline` 注入 `AddService`。
- Search 注入 QueryAnalyzer、RelationExpander 和 EvidenceReranker。
- Reranker 初始使用确定性 `LexicalReranker`，避免额外模型不确定性；后续公开实验可以替换。

应用状态必须暴露非敏感的 profile、模型名和 Prompt 版本，供内部部署审计使用，但不得新增比赛公共 HTTP 路由。

## 8. 数据流与降级

### 8.1 正式 MASM Add

```text
官方 Add
  → 媒体与 Schema 校验
  → 三智能体 AddPipeline
  → 结构化摘要与治理动作
  → text-embedding-v4 文本向量
  → gpt-4o-mini 图片文本化
  → text-embedding-v4 图片向量
  → 现有 fencing/lifecycle 事务提交
  → 立即可检索后返回 200
```

智能体输出失败时，现有 AddPipeline 可以降级为原始证据和基础摘要；但正式 Embedding 依赖失败时不能提交成功，因为成功后必须立即可检索。模型不可用错误应在 API 层映射为 HTTP 503，并保持账本和暂存对象清洁。

### 8.2 正式 MASM Search

```text
官方 Search
  → 查询分析（复杂文本可调用 gpt-4o-mini）
  → 文本/图片向量化
  → 关键词 + 向量 + 元数据混合召回
  → 一跳关系扩展
  → 冲突感知重排
  → 响应大小与 top_k 打包
  → 仅返回原始记忆证据
```

查询分析失败时回落到现有规则分析。Embedding 失败时返回 503，不得返回跨向量空间或 Fake 结果。

## 9. 测试策略

新增测试至少覆盖：

1. 正式档位缺少任一密钥时启动失败，且错误不含已有密钥值。
2. `local-fake` 不构造外部客户端。
3. Embedding 请求字段、批量顺序、维度和模型版本正确。
4. Embedding 响应乱序能够恢复，缺项、重复、NaN、错误维度被拒绝。
5. 图片被转换为受支持的 Data URI，并经过 `gpt-4o-mini` 描述后进入 `text-embedding-v4`。
6. `official-masm` 的 HTTP `/add` 确实调用三个智能体，而 `official-baseline` 不调用。
7. `official-masm` 的 Search 确实装配查询分析、关系扩展和重排，但仍只返回证据。
8. Provider 失败映射为 503，数据库没有遗留 PROCESSING，图片暂存目录没有泄漏。
9. 重复 `request_id` 回放不重复调用模型。
10. 既有 410 项测试、Ruff 和 Mypy 全量通过。

测试严禁读取真实环境密钥或发起真实公网请求。

## 10. 部署与实验衔接

`.env.example`、Compose 和部署文档补充正式档位配置，但所有值使用无效占位符。部署前运行一个只检查配置完整性的命令，再运行本地 Mock/Smoke 和公网官方 Smoke。

实验配置与运行档位一一对应：

- B0 → `official-baseline`
- MASM → `official-masm`
- 消融 → 从 `official-masm` 关闭单一组件，且在 manifest 记录关闭项

正式实验开始前必须把 `record-before-run` 替换为真实模型版本、Prompt 版本、commit、成本和部署 profile。小型公开子集先用于功能和参数选择，完整公开集只在候选版本稳定后运行。

## 11. 文档与合规

README 不再笼统声明“完整 MASM 已启用”，而应明确区分已实现组件与当前运行档位。部署文档必须说明：

- 正式档位的模型和版本。
- 两类密钥的用途和注入方式。
- 模型请求会处理比赛内容，但日志不保留原文、Base64 或 Prompt 载荷。
- Provider 故障的 503 和重试语义。
- `local-fake` 只能用于开发，禁止用于正式提交。
- 模型规则仍需在提交前向组委会确认，尤其是多模态图片文本化是否满足 Embedding 限制。

## 12. 验收边界

本轮代码验收完成后，系统应具备“只需注入合法密钥即可运行真实模型”的能力。没有真实密钥时，可以证明请求构造、装配、错误处理和持久化边界正确，但不能宣称真实模型效果、成本、延迟或官方 Smoke 已通过。

后续工作按以下顺序进行：注入测试密钥 → 小型真实模型 Smoke → 公开数据 B0/MASM/消融 → 公网部署 → 官方 Smoke → 冻结版本 → Full。
