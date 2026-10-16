# MASM v1.1 Evidence Graph — 本地实现交接（DeepSeek，2026-10-15）

**工作树（绝对路径）：** `C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge`

**分支：** `codex/masm-v11-evidence-graph`

**设计基线：** `2380446`（`docs: design grounded evidence graph handoff`）

**本报告对应 HEAD：** `a2e000c96bfa6112edb4726f5c2e457f69749567`（提交 C6 前的代码 HEAD；C6 只新增本文件与
`.env.example` 注释，不含任何 `src`/`tests` 改动）

本文只描述本轮本地实现、测试证据与限制。它不是部署许可，也不代表任何官方得分。

> **Codex 独立复核更新：** 第 14 节记录了 `fff7bd4` 之后的独立审查、RED→GREEN 修复和
> 最新本地门槛；其中的提交、测试计数与剩余风险取代本文较早的 DeepSeek 完成结论。

## 1. 范围与不变契约

实现的是默认关闭的 query-time grounded evidence graph：模型只提出问题需求、候选原文引用与
候选间链接；确定性代码负责 quote grounding、真实边验证、连通性、完整/部分/不足三态与最终证据组装。

保持不变（未修改、未弱化）：

- `evidence-selector-v7`（`src/masm/retrieval/evidence_selector.py` 零改动）仍是默认路径与唯一回退；
- 公共 `/add`、`/search`、健康检查、认证与响应模型；
- `top_k <= 100`、响应字节上限、16 并发等既有配置边界；
- 原始文本、多模态资产与来源顺序；不使用模型生成摘要作为公开证据；
- 存储 schema 与 Alembic 迁移（无 migration，`alembic/` 与 `storage/models.py` 零改动）；
- v1.0 分支、容器、卷与路由；部署与 Compose 文件零改动。

## 2. 提交列表（`2380446..a2e000c`）

| # | 提交 | 内容 |
| --- | --- | --- |
| C1 | `ac63ca9` `test: specify grounded evidence graph behavior` | 冻结基线上的 RED 规格：图行为、grounding/结构规则、固定 26 例（13 配对）合成套件与探针 |
| C2 | `2ab5bba` `feat: add grounded evidence graph planner and validator` | 严格 proposal schema、单次 planner 调用、`GraphContext`、归一化/grounding/discriminative-unit/结构校验 |
| C3 | `5420b43` `feat: assemble evidence from validated graph paths` | 连通分量、最短路径、三态推导、12 项上限降级、`GraphEvidenceSelector`（含纯图片直连与一次性回退） |
| C4 | `07acc3c` `feat: wire opt-in evidence graph search` | 配置开关与启动校验、provenance sidecar、只读 `relation_edges`、Search/runtime/app 接线、固定枚举遥测 |
| C5 | `a2e000c` `test: add evidence graph integration and safe probe` | 真实 repository/Search 集成测试 + 种子前沿 provenance 修复 + 探针失败模式 |
| C6 | 本文件 + `.env.example` 注释 | `docs: hand off evidence graph candidate` |

每轮提交都通过了当时的 focused 测试；C1 是刻意的 RED 提交（见 §5）。

## 3. 变更文件与 diff 统计

`git diff --stat 2380446`（C5 之后、C6 之前）：**20 files changed, 5279 insertions(+), 16 deletions(-)**

新增源码（3）

- `src/masm/retrieval/evidence_graph.py`：严格 Pydantic proposal schema、`EvidenceGraphPlanner`、
  `GraphContext`、`GraphDiagnostics`、`GraphSelection`、`GraphEvidenceSelector`、
  `source_pairs_from_edges`。
- `src/masm/retrieval/evidence_graph_rules.py`：归一化、quote grounding、discriminative unit、
  结构/边校验、固定失败类别。
- `src/masm/retrieval/evidence_graph_assembly.py`：连通性、最短路径、三态、冲突与上限处理。

修改源码（8，均为 additive 或接线）

- `src/masm/storage/types.py`：新增只读 `RelationEdge`。
- `src/masm/storage/repositories.py`：新增只读 `relation_edges()`（SQL 阶段按 `user_id` 过滤，
  并校验两端都是该用户记忆）；既有读写语义不变。
- `src/masm/retrieval/relation_expander.py`：新增 `expand_with_provenance()`/`ExpansionEdge`/
  `ExpansionResult`；`expand()` 返回类型、顺序、上限与查询模式不变。
- `src/masm/retrieval/diagnostics.py`：新增 13 个 `graph_*` 字段（固定枚举/计数/时延）。
- `src/masm/services/search_service.py`：一个可选 `graph_selector` 依赖 + `GraphContext` 构造 +
  遥测字段；关闭时调用路径不变。
- `src/masm/config.py`：5 个新设置与启动时依赖校验。
- `src/masm/runtime.py`、`src/masm/api/app.py`：按开关装配/注入图选择器。

新增测试（6）与既有测试改动（1）

- `tests/unit/retrieval/test_evidence_graph.py`（1455 行）
- `tests/unit/retrieval/test_evidence_graph_rules.py`
- `tests/unit/retrieval/test_evidence_graph_wiring.py`
- `tests/unit/retrieval/test_relation_expander_provenance.py`
- `tests/unit/test_evidence_graph_probe_script.py`
- `tests/integration/test_evidence_graph_pipeline.py`
- `tests/unit/retrieval/test_diagnostics.py`：日志 payload **精确键集**由 19 个扩展为 32 个
  （仍是 `set(payload) == {...}` 全等断言，未放宽任何安全门槛）。

新增脚本与文档：`scripts/evidence_graph_probe.py`、本文件、`.env.example` 注释。

## 4. 关键接口

```python
# evidence_graph.py
PROMPT_VERSION = "evidence-graph-planner-v1"
QuestionRequirement / GroundedSupport / ProposedLink / EvidenceGraphProposal   # extra="forbid", frozen, StrictInt
GraphContext(relation_source_pairs=(), source_context_enabled=False)
GraphDiagnostics(enabled, planner_calls, candidate_count, requirement_count, support_count,
                 link_count, component_count, selected_count, selected_source_count,
                 state, fallback, failure_category, planner_latency_ms)
GraphSelection(selection: SelectionResult, graph: GraphDiagnostics)

EvidenceGraphPlanner(llm, *, max_candidates, max_chars_per_candidate,
                     max_requirements, max_supports, max_links, timeout_seconds)
    .plan(question, visual, options, pool) -> EvidenceGraphProposal      # 每次 Search 恰好一次

GraphEvidenceSelector(planner, fallback, *, max_candidates=32, max_selected=12)
    .select(query, options, ranked, strong_anchor_ids, graph_context,
            *, request_user_id) -> GraphSelection

# evidence_graph_rules.py
normalize_text / grounded_quote / discriminative_units / shared_discriminative_unit
planner_candidate_text / CandidateView / candidate_views
ValidatedSupport / ValidatedLink / ValidationOutcome / validate_proposal(...)

# evidence_graph_assembly.py
assemble(*, requirement_count, supports, links, candidates, max_selected=12) -> AssemblyOutcome

# relation_expander.py（additive）
ExpansionEdge(predecessor_id, candidate_id, relation_type, depth)
ExpansionResult(candidates, edges)
RelationExpander.expand_with_provenance(user_id, seeds, limit) -> ExpansionResult
RelationExpander.expand(user_id, seeds, limit) -> list[MemoryCandidate]   # 行为不变

# repositories.py（additive，只读）
MemoryRepository.relation_edges(user_id, memory_ids, limit) -> list[RelationEdge]
```

**planner 只接受三类可证明连接：** 同来源相邻上下文（要求本请求启用了来源上下文且
`|Δsource_position| == 1`）、provenance sidecar 中真实存在的持久化关系来源对、满足
discriminative-unit 规则的显式跨来源 mention。strong anchor、相似度、排名接近、option、
共享泛化主题一律不作为连接证明；`strong_anchor_ids` 在 graph 路径中只用于 v7 回退。除此之外，
组装器可从同一需求下已 grounding 的正/负支持和相同非空存储 `conflict_group_id` 确定性派生内部
冲突边；planner 不能声明这种边，`graph_link_count` 也不把它计为 planner link。

**三态由确定性代码推导：** 覆盖要求必须有 validated `direct`/`negative`（`bridge`/`context`
只连接不覆盖）；多要求必须落在同一连通分量；`sufficient` 且证据 ≤ 12；否则 `partial`，只返回
含 direct 支持的最佳连通分量；无覆盖则 `insufficient`，返回空且**不回退**。正负冲突只有在同一
存储 conflict group 时才允许判为完整，否则保留双方并降级为 partial。

## 5. RED 证据（C1，冻结基线 `2380446`）

```
python -m pytest tests/unit/retrieval/test_evidence_graph.py \
  tests/unit/retrieval/test_evidence_graph_rules.py \
  tests/unit/test_evidence_graph_probe_script.py -q
-> 3 errors during collection
   ModuleNotFoundError: No module named 'masm.retrieval.evidence_graph'
   exit code 2
```

C2 落地后：`test_evidence_graph_rules.py` 由 RED 转 GREEN（28 passed），选择器级用例仍因
`GraphEvidenceSelector` 缺失而 collection error（刻意分阶段）。C3 落地后 focused 套件全绿。

## 6. GREEN 证据（本轮实际运行，非旧数字）

| 命令 | 实际结果 |
| --- | --- |
| focused 图/相关测试（9 个文件） | **`157 passed, 2 warnings`**，exit 0 |
| `python -m pytest -q`（全量） | **`939 passed, 1 skipped, 92 warnings`**，exit 0 |
| `python -m ruff check .` | `All checks passed!`，exit 0 |
| `python -m mypy src scripts/evidence_graph_probe.py` | `Success: no issues found in 61 source files`，exit 0 |
| `git diff --check 6ad49ad3e99b725821e5e9d1b053d2a0148fe319..HEAD` | 空输出，exit 0 |

跳过项是既有的 Windows POSIX 权限语义测试（`test_cloud_smoke_recovery_manifest.py:114`），
与本次改动无关。92 条告警全部是既有依赖/`asyncio` 弃用告警。

最新 focused 命令与全量命令使用的解释器：
`C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge\.venv\Scripts\python.exe`
（Python 3.14.5，pytest 8.4.2，ruff 0.17.0，mypy 1.20.2）。

分阶段记录：

- C2：`test_evidence_graph_rules.py` → `28 passed`；selector 级用例仍 RED。
- C3：focused 图套件 `74 passed`；`tests/unit -q` → `663 passed, 1 skipped`。
- C4：`tests/unit tests/contract -q` → `740 passed, 1 skipped`；
  `tests/integration tests/isolation -q` → `179 passed`。
- C5：全量 `924 passed, 1 skipped`。

## 7. 设计规格第 11 节覆盖

| # | 用例 | 位置 |
| --- | --- | --- |
| 1 | direct 单来源 | `test_evidence_graph.py::test_direct_single_source_fact_is_sufficient` |
| 2 | 双属性（同来源相邻 / 跨来源需可证链接） | `test_two_attributes_in_one_source_are_sufficient_through_adjacent_context`、`test_two_attributes_from_two_sources_without_a_link_stay_partial`、`test_two_sources_connect_only_through_a_discriminative_mention` |
| 3 | 显式跨来源 `Nora -> Zephyr workshop -> Lisbon` 链 | `test_explicit_cross_source_chain_is_sufficient` |
| 4 | 同来源相邻省略确认 | `test_elliptical_confirmation_returns_the_adjacent_question_turn` |
| 5 | 前向纠正（纠正值胜出且两段 quote 可用） | `test_forward_correction_keeps_both_turns`、`test_unproven_correction_link_is_rejected` |
| 6 | 持久化两跳 + provenance sidecar | `test_persisted_relation_requires_the_provenance_sidecar`、集成 `test_persisted_relation_sidecar_enables_a_cross_source_chain` |
| 7 | 错误主体（外观完整但拒答） | `test_wrong_subject_bridge_only_graph_abstains` |
| 8 | 断链 + 泛化名词 | `test_generic_shared_noun_cannot_connect_disconnected_candidates` |
| 9 | 缺失值 | `test_missing_requested_value_abstains` |
| 10 | option 干扰 | `test_multiple_choice_options_cannot_create_support` |
| 11 | 高排名 + strong-anchor 干扰 | `test_high_ranked_strong_anchor_distractor_is_excluded`、`test_strong_anchors_alone_never_produce_evidence` |
| 12 | 拥挤 32 候选正/负 | `test_crowded_32_candidate_positive_case_returns_only_the_chain`、`test_crowded_32_candidate_negative_case_abstains` |
| 13 | 冲突组保留双方 | `test_conflicting_support_in_one_stored_conflict_group_keeps_both_sides`、`test_conflicting_support_outside_a_conflict_group_is_partial` |
| 14 | 重复来源表示只计一次 | `test_duplicate_representation_of_one_source_counts_once` |
| 15 | 中文 direct/上下文/断链 | `test_chinese_direct_fact_is_sufficient`、`test_chinese_contextual_confirmation_is_sufficient`、`test_chinese_generic_workshop_noun_does_not_connect` |
| 16 | 非法 schema/quote/index 与超限 | `test_evidence_graph_rules.py` + `test_structurally_invalid_proposals_fall_back_once`（6 类结构失败） |
| 17 | planner 回退 | `test_planner_transport_failure_falls_back_to_the_frozen_selector_once`、`test_structurally_invalid_proposals_fall_back_once` |
| 18 | valid-insufficient 不回退 | `test_valid_insufficient_graph_never_falls_back` |
| 19 | 纯图片不回归 | `test_pure_image_query_keeps_the_existing_path`、`test_pure_image_query_preserves_user_scoped_anchor_beyond_graph_pool` |
| 20 | 遥测不泄漏 | `test_evidence_graph_wiring.py::test_graph_diagnostics_are_content_free`、`test_suite_output_never_contains_question_or_candidate_text`、`test_diagnostics_sanitize_unrecognized_enums` |
| — | 属性式（确定性顺序、环、菱形、置换、12 项上限） | `test_cycles_and_diamonds_terminate_with_one_component`、`test_selection_is_stable_when_unrelated_candidates_are_permuted`、`test_complete_graph_over_the_evidence_cap_degrades_to_bounded_partial` |
| — | 真实 repository/Search（渲染一致、跨用户边拒绝） | `tests/integration/test_evidence_graph_pipeline.py` 全部用例 |
| — | 固定合成套件（≥20 例、正负配对、fake provider） | `scripts/evidence_graph_probe.py` + `tests/unit/test_evidence_graph_probe_script.py`（26 例 / 13 配对） |

## 8. 隔离、隐私与遥测

- 所有新增 SQL 读取（`relation_edges`）在 SQL 阶段按 `user_id` 过滤，并额外要求关系两端都是
  该用户的记忆；损坏行或错误 fake 提供的跨用户边在 repository 与 Search 两层都被丢弃。
- graph 选择器在构建候选池之前按请求用户过滤 `ranked`；跨用户候选永远不会进入 planner payload。
- 诊断只记录固定枚举（`graph_state`、`graph_failure_category`）、计数与耗时；未识别的取值折叠为
  `unknown`。测试用 canary 断言日志与探针输出既不含问题、候选正文、quote，也不含 ID、request_id、
  用户或 provider 细节。
- `GraphContext` 只在确定性校验器内部使用，planner 只看到不透明来源标签（`source-1` 起）。

## 9. 默认关闭证明

- 新开关默认 `0`，且只接受 `"1"`（与仓库既有布尔约定一致）。
- graph 关闭时 `runtime.evidence_graph is None`，`SearchService` 不持有 graph 选择器、不构造
  `GraphContext`、不调用 `expand_with_provenance`，planner 调用数恒为 0（单元与真实数据库集成
  测试各有一条断言）。
- `expand()` 的返回类型、顺序、上限与查询次数不变（既有 21 条 expander 测试原样通过）。
- 配置冲突在 `validate_runtime()` 启动时拒绝：graph 开启但缺少 v7 回退、缺少来源上下文、
  非 `official-masm` 档位，或数量/超时越过硬上限。

## 10. 临时数据库/容器

- 本轮启动的唯一容器：**`masm-v11-evidence-graph-pg`**（`pgvector/pgvector:pg16`，仅映射
  `127.0.0.1:5433`，`--tmpfs /var/lib/postgresql/data`，`AutoRemove=true`，
  `POSTGRES_HOST_AUTH_METHOD=trust`，因此全程没有使用任何密码或完整连接串）。
- 启动后只读核实：`current_database()` = **`masm_test`**，PostgreSQL 16.15，`alembic_version` = `0005`。
- 未触碰本机既有的其他容器（含已停止的 `masm-v11-testdb`）与线上容器。
- 全量测试后只读核对本轮测试用户前缀（`graph-%`、`edge-%`、`diag-%`、`context-%`）：
  `memories=0`、`source_messages=0`、`request_ledger=0`、`assets=0`、`memory_relations=0`。
  `users` 表仍有 9 行：`delete_run` 按既有设计不删除用户行，仓库既有隔离测试同样采用
  唯一前缀隔离，这不是本次改动引入的残留。
- 容器在本报告定稿后停止（`docker stop masm-v11-evidence-graph-pg`，tmpfs 数据随
  `AutoRemove` 一并删除），只停止本轮启动的这一个容器。
- Codex 复核另行启动 `masm-v11-review-fixes-pg`（同一镜像，仅回环端口 5434、数据库名
  `masm_test`、tmpfs、AutoRemove），用于最新 integration/full 门槛；完成后已停止并自动移除。
  既有 `masm-v11-testdb` 仍保持原先的 Exited 状态且未被启动、修改或删除。

## 11. 未运行事项声明

本轮**没有**执行、也没有授权执行：真实 Provider 调用、官方 Smoke、Full 评测、容量测试、
任何部署/推送/push、Docker 镜像构建、服务器或线上容器/数据库/域名访问、线上日志读取、
隐藏评测内容读取、数据库迁移之外的任何生产写入。`scripts/evidence_graph_probe.py` 的
真实 Provider 模式本轮未运行。

## 12. 已知限制与尚未满足的门槛

1. **gate 5（真实 Provider 稳定性）未做**：固定 26 例套件 5 轮全通过、零 provider/schema 失败需要用户
   授权后另行执行；本轮只证明了 fake provider 下的确定性。
2. **gate 6（真实 Add→Search 探针）部分完成**：真实数据库的 Add→Search 路径已覆盖直接事实、
   跨来源链、断链、跨用户边与渲染一致；图片/多模态清理、原子检索与完整删除残留核验仍需按
   发布 runbook 再跑。
3. **gate 7（16/16 并发）未做**：本机未跑并发与容量门槛。
4. **弱主体保护依赖 proposal 质量**：确定性代码只能校验引用与连接是否可证，无法判定
   “My colleague moved to Porto”是否答非所问；这类拒答取决于 planner 是否给出 `direct`/`negative`
   之外的角色。合成套件把这一行为固定在负例上，但真实模型行为未验证。
5. **连通性要求使“多要求但互不相关”的问题更保守**：规格要求多要求必须落在同一连通分量，
   因此两个来源各答一半且无可证链接时判 `partial`，只返回最佳分量。这是刻意选择（宁可少答不可
   假完整），但可能影响 direct recall，需在官方 Smoke 上验证。
6. **持久化关系边只到来源分组粒度**：sidecar 把记忆级关系边折算为来源对，因此“同两个来源之间的
   任意候选”都能被该边连接。这是设计规格第 7.3 节的要求，但其召回影响未在真实数据上评估。
7. **`audit_metadata()` 未新增图开关字段**：为避免改动既有精确断言的运行档位测试，本轮选择不加；
   部署侧可从环境变量与 `/search` 遥测确认模式。
8. **冲突组只产生内部确定性连接**：`_complete_conflicts` 仍不伪造 relation sidecar；只有同一需求
   下通过 quote/user 校验的正负支持且存储 `conflict_group_id` 相同，组装器才派生内部边。该边不扩展
   planner schema，也不计入 `graph_link_count`。

## 13. 建议下一步

1. Codex 独立复核 diff 与证据（本文件不构成通过结论）。
2. 用户授权后：在真实 Provider 上执行 `scripts/evidence_graph_probe.py --stability`（固定 26 例、
   13 个正负配对、单进程完整 5 轮；任一轮失败都返回非零，不得挑轮次）。

   服务器上应使用既有秘密注入机制，不要把值写入命令、仓库或回传日志。当前 `Dockerfile`
   有意不复制 `scripts/`，而 Provider 配置只注入 API 容器；因此不要因容器内缺少 probe 就复制文件、
   重建镜像或把秘密导出到宿主机。先核对 API 容器内已有
   `MASM_RUNTIME_PROFILE=official-masm`、`MASM_LLM_BASE_URL`、`MASM_LLM_API_KEY`、
   `MASM_EMBEDDING_BASE_URL`、`MASM_EMBEDDING_API_KEY`，再从已核验的候选 checkout 把只读 probe
   经标准输入送入容器。运行前还必须用候选清单中的镜像 ID/源码指纹确认容器内安装的 `masm`
   与该 checkout 一致；成功导入模块不等于版本一致。仓库内的服务器门槛脚本已固定上述检查，
   默认模式只读且不调用 Provider：

   ```bash
   python3 scripts/evidence_graph_server_gate.py
   ```

   仅当其输出 `verification_exit_code=0` 后，才在已接受至少 130 次真实 LLM 请求的负载和费用的维护
   窗口显式执行：

   ```bash
   python3 scripts/evidence_graph_server_gate.py --stability
   ```

   脚本从候选 checkout 经标准输入运行 probe，不在容器内落盘、不重启服务、不把 Provider 变量导出
   到宿主机。交互模式始终以进程状态 0 返回，真实门槛状态只看输出中的 `verification_exit_code` 和
   `probe_exit_code`，因此外层启用 `set -e` 也不会因门槛失败结束 SSH shell；自动化若确实需要非零
   进程状态，必须显式增加 `--strict-exit`。完整结果文件默认在系统临时目录中独占创建且不回显路径；
   写入或关闭失败会返回固定状态，但可能留下空文件或部分文件。若确需保留到已知位置，可指定一个
   checkout 外、尚不存在的路径，例如
   `--output /tmp/masm-evidence-graph-stability-manual.jsonl`。脚本拒绝 checkout 内路径、已有文件和
   最终路径符号链接。终端只额外显示最后一行 `stability_summary` 和 `probe_exit_code`。只需回传这些
   状态行；不要回传环境变量、Provider 响应或其他服务日志。若容器名不同，可使用
   `--container NAME`；若 SSH 本身不稳定，应使用服务器既有的会话托管机制，前台命令不会自动跨网络
   断线继续运行。
3. 通过后按设计规格第 13 节做 API-only canary（记录镜像/容器 ID，仅替换 API 容器，Postgres 容器
   ID 必须不变），最多一次官方 Smoke；Full 不运行。
4. 若需要，再补：并发 16/16、纯图片与混合多模态 Add→Search 清理核验、端到端延迟与响应字节核对。

## 14. Codex 独立复核与修复（`fff7bd4` 之后）

### 14.1 实际发现与处理

| 严重度 | 独立复现 | 处理 |
| --- | --- | --- |
| P1 | planner 失败时把 40 个原始 ranked 交给 v7，触发 32→48 深层重试，总计 3 次 provider 调用 | 所有失败分支只把同一份 user-scoped、最多 32 项的 graph pool 交给 v7；回归测试固定为 graph 1 次 + v7 1 次 |
| P1 | 同一存储冲突组的正/负支持若无 planner link，只返回一侧却报告 sufficient | 从已 grounding 的正/负支持和存储冲突组派生内部确定性邻接；双方必须共同进入解释，planner link 计数仍为 0 |
| P1 | 14 节点完整链超出 12 项上限时保留首尾却删除桥节点，返回断开的 partial | 超预算时枚举可覆盖的需求子集与支持选择，只返回预算内的确定性连通路径并按覆盖/直接支持/来源/大小/rank 排序 |
| P2 | selector 从 `ranked[0].user_id` 推断请求用户；foreign-first 可反转作用域 | `SearchService` 显式传 `request_user_id`，selector 在建池前过滤，validator 再独立核对候选 user |
| P2 | 合法支持组合超过 512 时只检查各需求第一个节点，可能错过后续唯一连通解 | 利用 4 requirements/24 supports 硬上限，完整枚举最大 `6^4 = 1296` 种组合 |
| P2 | `relation_edges()` 无 SQL `LIMIT`，且读取任一端命中的全部高出度边，外部 frontier 边可挤掉目标边 | SQL 同时要求双端位于 traversal set、双端属于请求用户，再稳定排序和 limit；第二跳传入 bridge + discovered endpoint |
| P2 | SQLAlchemy 异常可打印绑定参数；pytest 失败可通过 `Settings.__repr__` 打印数据库地址和 API keys | `Database.create(..., hide_parameters=True)`；`Settings.database_url`/`api_keys` 设为 `repr=False`，均有 canary 回归测试 |
| P2 | 把纯图片直通 v7 也截断到 32 项 graph pool，会丢失更深但合法的 strong image anchor | 纯图片不属于 planner 失败；零 planner 调用地保留完整 user-scoped ranked 输入，只有真实 graph 失败回退受 32 项限制 |
| P2 | 测试数据库 URL 即使通过 `masm_test` 校验，仍可在 pytest 失败值表示中完整出现 | 校验后返回行为等同 `str` 但 `repr` 固定脱敏的值，canary 同时固定字符串兼容性和脱敏 |
| P2 | 真实 Provider 门槛只能单轮运行，缺少一次性五轮聚合，手工循环容易挑轮次或提前停止 | 新增 `--stability`：单进程固定运行五轮完整套件，不因单轮失败提前退出，只有 5/5 轮全部通过才返回 0；与调试用 `--reverse` 互斥 |

以上修复没有新增 public schema、migration、planner rationale/answer/state/selected indices，也没有修改
默认关闭、local-fake、渲染、删除、多模态、响应预算或 16 并发实现。

### 14.2 复核提交

| 提交 | 内容 |
| --- | --- |
| `c219adc` | `docs: plan evidence graph review fixes` |
| `009d81e` | `fix: enforce evidence graph request user scope` |
| `6ca7477` | `fix: bound evidence graph fallback to one v7 call` |
| `823e517` | `fix: enumerate bounded evidence graph combinations` |
| `c7eac89` | `fix: keep bounded partial evidence connected` |
| `739f0aa` | `fix: preserve both sides of stored conflicts` |
| `c3b5421` | `fix: bound relation provenance in SQL` |
| `8102c2f` | `fix: redact sensitive values from failures` |
| `aff3fa5` | `fix: preserve pure image selector behavior` |
| `d0746f7` | `test: redact test database URL repr` |
| `0348d78` | `test: add fixed provider stability gate` |

固定 Provider stability 门槛的后续安全修复代码/测试 HEAD：`7d3ceb2`。它确保已验证 probe 以原始
字节送入容器、默认临时目录不会落入 checkout，并把 write/flush/close 失败转换为脱敏状态。第三轮
独立只读复审未发现新的 Critical/Important，且确认上一轮三项 Important 均已关闭。最新全量本机
回归为 **954 passed、1 skipped、92 warnings**；Ruff、mypy（62 个源文件）和 diff-check 均通过。
完整 HEAD 以交付消息中的实际 `git log` 为准。

### 14.3 最新资格判断

本地 deterministic、数据库 integration、隔离、契约、Ruff、mypy 与 diff-check 门槛全部通过，
因此该分支可作为“进入下一阶段 Provider 稳定性门槛”的候选。`--stability` 已把五轮固定门槛收敛为
单进程执行；门槛状态由 `probe_exit_code` 表示，只有显式 `--strict-exit` 才把失败映射为非零进程状态。它仍未通过真实 Provider 五轮固定
套件、API-only canary 或官方 Smoke；不得据此宣称官方分数提高，也不得直接部署。

## 15. 服务器门槛第一次运行的结论与 pin 修复（2026-10-17）

### 15.1 首次运行结果

服务器 checkout `/root/AgentMemoryChallenge` 位于 `d022b8a`（不是候选清单要求的提交），只读核验返回
`verification_exit_code=2`，因此 `--stability` 未启动，没有产生任何真实 Provider 调用。该退出码对应
`_verify_candidate` 的**前置失败分支**（`candidate_checkout` / `probe_worktree` / `source_manifest` /
`verification_error=command_failed` / `container_check=failed` / `verification_error=invalid_output`），
原因行被运维侧过滤表达式漏掉，需从服务器上的 `/tmp/masm-eg-verify.txt` 读取。

### 15.2 独立发现：pin 与门禁比对口径不一致

即使前置失败被修复，`d022b8a` 的 `source_digest` 仍会失败，因为门禁比较的是**已提交字节（git blob）
摘要**与**容器内已安装文件摘要**，而当时的 pin 既不是两者之一：

| 对象 | 值 |
| --- | --- |
| `d022b8a` 的旧 pin | `541fceac…3e3dd`（与任何一端口径都不符） |
| 中间提交 `2eddbb8` 的 pin（Windows worktree 字节，含 CRLF） | `8b81c490…5c9b8da` |
| 已提交 blob 摘要 = 容器内已安装摘要（平台无关） | `d1e1ce2b…b8b6b79` |

本地复现：`core.autocrlf=true` 时 `src/masm` 有 55/60 个文件带 CRLF，`_package_digest`（worktree 字节）
因此与 `_git_package_digest`（`git cat-file blob`）不等；容器模板计算的是已安装文件的 LF 字节。

### 15.3 修复（`9b24224`）

- `source_digest` 改钉**平台无关的 blob 摘要** `d1e1ce2b…b8b6b79`；
- 新增 `_probe_bytes_match()`：送进容器的 probe 在比较前做 CRLF→LF 归一化，Windows 与 Linux
  checkout 都能通过，而任何内容改动仍然失败（已提交内容另由 `probe_blob` 独立钉住）；
- 测试改为断言平台无关口径：`_git_package_digest` 等于 pin、CRLF worktree 仍等于 HEAD
  （`_worktree_matches_head`）、LF/CRLF probe 均匹配且被篡改的 probe 不匹配。

RED：`-k "platform_independent or line_ending or pins_the_committed"` → 3 failed（缺 `_probe_bytes_match`
且 pin 不等于 blob 摘要）。GREEN：该测试文件 `28 passed`；全量 `973 passed, 1 skipped, 92 warnings`；
Ruff 通过；mypy 62 个源文件通过。

### 15.4 已发布历史与复跑前置条件

推送时发现 origin 上已存在两个同内容的重复提交（`f1d91ef`、`8d1be10`），因此本地等价的两份被
rebase 丢弃，只保留三个独有提交：`2eddbb8`（中间 pin）、`9b24224`（本修复）、
`1b67ac5`（本节文档）。rebase 前后整棵树逐字节一致（`git diff` 为空），已发布 HEAD 为 `1b67ac5`。

1. 服务器 checkout 必须包含 `9b24224`（或更新的同分支提交），即 `1b67ac5`。
2. `src/masm` 在 `d022b8a..1b67ac5` 之间没有任何改动，因此**不需要重建镜像**：容器内已安装摘要
   本来就等于新 pin，只需更新 checkout 与 probe。
3. 复跑前先读 `/tmp/masm-eg-verify.txt` 确认 `verification_exit_code` 的具体原因（容器名、运行档位或
   Provider 变量），再执行 `--stability`。

## 16. 首次真实 Provider 稳定性门槛结果：候选被拒绝（2026-10-17）

### 16.1 运行事实

候选镜像由 `537c892` 构建并只替换 `masm-v11-api-1`（`--no-deps`，graph 与来源上下文开关保持关闭），
`masm-v11-postgres-1` 容器 ID 未变；只读核验 `verification_exit_code=0`（`source_match=yes`、
`candidate_modules=present`、`runtime_settings=valid`、四个 Provider 变量 `set`）。随后一次性五轮固定
门槛（26 例 × 5 轮 = 130 个用例行）：

| 指标 | 实际值 |
| --- | --- |
| `round_count` / `rounds_passed` | 5 / **0** |
| `probe_exit_code` | 1 |
| `candidate_eligible` | false |
| `failure_categories` | `none` 121、`planner_invalid` 9；**无 `planner_unavailable`** |
| `fallbacks` | 9（全部集中在两个 persisted 用例） |
| `evidence_states` | sufficient 79、partial 16、insufficient 35 |
| 单例 `passed` | 94 / 130（任一轮必须 26/26 才算通过，故五轮全失败） |

按事先冻结的判定规则：**该提交被拒绝**，不重跑、不调参刷通过。因为不存在 `planner_unavailable`，
Provider 通路正常，所以这次运行对该候选是**有效测量**，不适用"基础设施无效运行"豁免。

### 16.2 失败用例与机制

| 用例 | 失败轮次 | 观测 | 机制 |
| --- | --- | --- | --- |
| `two_attributes` | 5/5 | partial，`link_count=0`，只返回 1 条 | 模型没有为同来源相邻两条提出 `same_source_context`，两条各自成单节点分量 |
| `cross_source_chain` | 5/5 | **sufficient**，`link_count=0`，只返回 1 条 | 模型把“哪个城市 + Nora 参加”压成单一需求，只用 1 条 direct 支持就判完整，另一片段缺失 |
| `crowded_candidates` | 5/5 | 同上（32 候选） | 同上 |
| `cross_source_chain_negative` | 5/5 | **sufficient**，返回含 `Lisbon` 的单条 | 同上：绑定子句（Nora 参加）无支持却判完整 → **假阳性（安全相关）** |
| `chinese_context` | 2/5 | partial，只返回 1 条 | 同上（同源/跨源连接缺失） |
| `chinese_context_negative` | 5/5 | sufficient/partial，返回含 `里斯本` 的单条 | 同上 |
| `persisted_two_hop` | 5/5 | `planner_invalid` + 回退 | proposal 未通过结构校验（原因枚举待 `--diagnostic` 一轮确认） |
| `persisted_two_hop_negative` | 4/5 | 同上 | 同上 |

通过的用例全部是单来源直接事实、省略确认、纠正、错误主体、缺失值、选项干扰、断连拒绝等；失败集中在
**多需求分解与跨来源连接**，即本架构要解决的目标场景。

### 16.3 结论与未决设计问题

1. 确定性层自身没有被证伪：同一套件在 fake provider 下 26/26 通过；真实失败来自模型提出的
   requirement/link 与真实语义之间的差距。
2. **架构级缺口**：`sufficient` 的安全性依赖模型把问题拆成完备的需求集。当前校验只检查
   “question_quote 是否原文出现在问题里”“candidate_quote 是否原文出现在候选里”，**不检查
   candidate_quote 是否与该需求有任何语义关系**，因此模型可以把任意已 grounding 的候选引用
   挂到任意需求上并宣称 direct 覆盖，从而在绑定子句无支持时仍判 sufficient（16.2 的假阳性）。
   纯字符串规则无法裁决这一点：例如 `two_attributes`/`cross_source_chain` 的合法用例里，
   需求引文（来自问题）与支持引文（来自候选）本就可能没有共同的区分单元。
3. 可在确定性层安全实施、且属于规格允许连接类型的改进（需用户批准后按 RED→GREEN 落地）：
   - R1：同来源相邻位置由代码直接建边（规格允许的连接类型 1，无需模型提出）；
   - R2：跨来源显式 mention 由代码在候选可见文本之间计算区分单元并建边（规格允许的连接类型 3）。
     两者都可能修复 `two_attributes`、`cross_source_chain`、`crowded_candidates`、`chinese_context`
     的召回，但**不能**保证修复两个期望拒答/部分的假阳性负例。
4. 线上状态：`masm-v11-api-1` 运行 `537c892` 且 `graph_flag=unset(default_off)`，真实 Search 仍走冻结的
   v7 路径；本次门槛失败没有改变线上行为，旧 override 仍可回滚。

## 17. R1/R2/R2b：把可证明连接从模型手里拿回来（`7e756a1`）

用户批准 R1+R2 后按 RED→GREEN 落地（RED：两个新连接测试先失败；GREEN：95 个图与规则测试通过）。

### 17.1 实现

| 规则 | 内容 | 规格依据 |
| --- | --- | --- |
| R1 | 同一非空来源、位置相差 1 的候选由代码直接建边；仅在本次请求可用来源上下文时成立 | 连接类型 1 + 失败规则「来源上下文不可用则上下文边不可用」 |
| R2 | 不同来源分组的候选，其**可见文本**共享一个问题中不存在的区分单元时由代码建边；该单元还必须在本次候选池内稀有（文档频率 ≤ 2） | 连接类型 3 |
| R2b | 选定包含集后，在同一连通分量内补入能覆盖「尚无任何已选候选覆盖的问题区分单元」的已证明邻居，确定性排序并受 `max_selected` 限制 | 组装规则「覆盖节点 + 最短已校验路径」的确定性延伸 |

同时把来源上下文 JSON 的结构键与字面量（`matched/messages/position/role/text/timestamp/user/assistant/null/...`）
加入既有受审 denylist：它们在每个来源里都重复出现，绝不构成区分单元。稀有度约束（≤2）用于挡住
“Grounded support …”“Unrelated note number …”这类重复措辞把无关来源连成一片。

### 17.2 效果与边界

- 新增 RED→GREEN 测试：模型**完全不提链接**时，同来源相邻（`two_attributes` 类）与跨来源共享区分单元
  （`cross_source_chain`/`crowded_candidates`/`chinese_context` 类）仍然连通并返回两侧证据；
- 守卫测试：泛化词/问题内词/JSON 结构词都不得建边，未连接候选不得被补入；
- 套件（fake provider）仍为 26/26、正负各 13/13、零回退；
- **仍未修复**（与实施前的判断一致）：两个期望拒答/部分的假阳性负例，以及 `persisted_two_hop` 的
  `planner_invalid` 类。因此在两者解决前，不应再花费一次付费资格门槛运行。

### 17.3 后续部署影响

`src/masm` 已变更，`source_digest` 重新钉为平台无关 blob 摘要
`eff1dd3fb74f4af75c43bf099084e74c06bb19fbbc2c05da0eb5bc326c051f2d`（`probe_blob`/`probe_sha256` 未变）。
任何新候选镜像必须由新提交构建，容器内安装摘要才会与 pin 一致。

## 18. 完整性闸门：只分解了一半的问题不得判为完整（`33ff242`）

### 18.1 规则与依据

对第 16 节的两个假阳性负例（期望拒答/部分）落地"问题完整性"闸门：

- `question_units`：问题的区分单元；`requirement_units`：每个已校验需求引文的区分单元；
- **未被任何需求主张、且没有任何已选证据覆盖**的问题单元若存在，则该组合不得判为 sufficient，
  部分分支直接返回 `insufficient` 且证据为空（`unclaimed - grounded(included) != empty`）。

CJK 单元必须先在本请求候选池中被实际证实（DF ≥ 1）才参与完整性判定：未分词的中文子串会产生
跨词边界的伪单元（问题"城市举办"里的"市举"），任何候选都无法覆盖它，若当作硬性要求会误杀合法完成。
拉丁单元（≥6 字符 token 与多词短语）直接参与。

### 18.2 被否决的更严版本（有意记录）

先实现过更严的版本："需求的每个区分单元都必须被已选证据覆盖，否则该需求不算被覆盖"。
它当场打断了既有测试 `test_unproven_correction_link_is_rejected`：问题"What color bicycle did I buy?"
本就可以用"No, I bought the red one."回答（省略回答不含 bicycle/color 字面词），该规则会在这类
合法召回上直接拒答——正是本项目历史上 direct recall 归零的失败模式。该规则已删除，代码注释与提交
信息都记录了原因。

### 18.3 仍然存在的缺口（需 Codex 评审或契约变更）

若模型不是**漏掉**绑定子句，而是把绑定子句**吞进需求引文**（单个需求引文横跨
"Which city hosts the workshop that Nora attended"），则 Rule A 保持沉默，负例仍可能判 sufficient。
这需要语义裁决：字符串规则无法区分"省略回答"与"无关候选"，可行的方向是改契约（例如要求需求引文
不得横跨两个内容子句，或要求模型为每个绑定子句单独出需求），属于提示词/契约决策，需要用户批准。

### 18.4 验证

- RED→GREEN：`test_unclaimed_question_content_blocks_sufficiency`、
  `test_chinese_negative_without_grounded_binding_content_abstains` 先失败后通过；
- 全量（临时 `masm_test`）：980 passed / 1 skipped，唯一失败是当时过期的 source digest pin（本节刷新）；
- 套件（fake provider）仍 26/26、正负各 13/13、零回退：Rule A 在**分解完整**的提案上从不触发，
  这是它不会伤及既有召回的直接证据；
- `source_digest` 重新钉为 `9fd4b38f1eef244c47446ada3e56f293ed447d035b8b81d8e4ea113a92fc069f`。

## 19. 固定枚举的结构失败原因：让下一次诊断一次定位（`3e74489`）

### 19.1 为什么必须补这个instrument

第 16 节的 JSONL 里，`persisted_two_hop`（5/5）与 `persisted_two_hop_negative`（4/5）都是
`planner_schema_valid=true` + `failure_category=planner_invalid` + 回退。但**回退路径把
`requirement_count`/`support_count`/`link_count` 全部清零**，因此无法判断是哪条结构规则触发
（空需求列表？下标越界？需求引文未落地？），也无法判断模型究竟发了什么形状。继续凭猜测改提示词
就是提示词轮盘赌，与项目既有教训相悖。

### 19.2 实现（纯诊断，规格 §10 允许：只有固定枚举与计数）

| 位置 | 新增 |
| --- | --- |
| `ValidationOutcome.reason` | 每个结构拒绝点写入固定枚举：`candidate_scope_mismatch`、`empty_requirements`、`requirement_count_exceeded`、`requirement_quote_not_grounded`、`support_count_exceeded`、`support_index_out_of_range`、`link_count_exceeded`、`link_index_out_of_range`、`link_self_reference` |
| `GraphDiagnostics.planner_failure_reason` | 另含 `none`、`schema_invalid`（解析失败）、`planner_unavailable`、`internal_error` |
| 搜索诊断载荷 | 新增 `graph_planner_failure_reason`，与其它图字段一样按固定枚举白名单折叠，其它取值折叠为 `unknown` |
| probe | 每条用例行输出 `planner_failure_reason`；每轮汇总输出 `planner_failure_reasons` 计数 |
| server gate | 只接受同一套固定键的 `planner_failure_reasons` 映射（键未知或计数不合即整份诊断作废） |

RED 证据：`test_structural_failures_report_a_fixed_reason` 六种形状（空需求、引文未落地、支持下标越界、
自链接、链接超上限、schema 失败）与 `test_successful_proposals_report_no_failure_reason` 先全部失败。

### 19.3 pin 与部署影响

`src/masm` 与 probe 都变了，因此三个 pin 全部刷新：

```
probe_blob    = 8cff3fbd1af934d1ba36905230f208ce4d7411e4
probe_sha256  = e02b2501ebf415d0a357ddb062785bd229f6874967a9727cc232c247a38f5f02
source_digest = ecfd0c52bc9b12ecbd8c8c056b7b895fe9047fb446a9e76d9a6e4ab502699a05
```

下一次诊断轮（1 轮 26 次调用，不产生资格结论）将同时回答两件事：
1. R1/R2/Rule A 实际修复了多少此前失败的用例（第 16 节共 8 个用例、36 个用例行失败）；
2. `persisted_two_hop` 的结构失败究竟是哪条规则，从而决定是改契约（例如空需求列表是否算合法弃答）
   还是按固定原因精确修复。任何资格门槛仍只在二者明确之后、由新提交跑一次。

## 20. 诊断轮结果与两个精确修复（`31fec64`）

### 20.1 诊断轮（1 轮 26 例，不构成资格结论）

`planner_failure_reasons = {"none": 24, "requirement_quote_not_grounded": 2}`，24/26 通过：

| 用例 | 上一轮（5 轮门槛） | 本轮诊断 | 结论 |
| --- | --- | --- | --- |
| `two_attributes` | 0/5 通过 | ✅ | R1 同来源相邻建边修复 |
| `cross_source_chain` | 0/5 | ✅ | R2 + R2b 修复 |
| `crowded_candidates` | 0/5 | ✅ | R2（池内稀有度 ≤2）+ R2b 修复 |
| `chinese_context` | 3/5 | ✅ | R2 + R2b 修复 |
| `cross_source_chain_negative` | 0/5 | ❌ sufficient | hyp-2 确认：`requirement_count=1`、`support_count=1`，绑定子句被吞进唯一需求引文 |
| `chinese_context_negative` | 0/5 | ❌ sufficient | 同上 |
| `persisted_two_hop` | 0/5 | ❌ `requirement_quote_not_grounded` | **不是**空需求列表，而是模型改写/标点了问题原文 |
| `persisted_two_hop_negative` | 1/5 | ❌ 同上 | 同上 |

即：第 16 节担心的"模型给出空需求列表"并未发生；两个留待决策的假设各有明确答案。

### 20.2 修复 1：引文标点容差 + 可定位的改写原因

需求引文的 grounding 现在允许**仅标点差异**（逗号、分号、问号、连字符写法）：先按归一化精确子串，
再按"去标点形状"子串匹配。改写与改序仍然拒绝，且原因细化为
`requirement_quote_reordered`（词元多重集相同、顺序不同）或 `requirement_quote_paraphrased`（词元不同），
从而让契约问题一次可定位。候选引文的 grounding 保持精确子串不变。

### 20.3 修复 2：子句标记门控的完整性收紧

仅当问题含**子句边界标记**（逗号/分号，或关系代词 that/which/who/whom/whose）时，需求引文自身的
区分单元必须被已选证据覆盖，否则该需求不算被覆盖；若因此没有任何需求被覆盖，则判 `insufficient`
且证据为空。这样 `cross_source_chain_negative`（关系从句）与 `chinese_context_negative`（中文逗号）
都被修复，而不影响单子句问题。

**为什么必须门控**：不分门控的版本此前已用既有测试证明有害——它会打断
`test_unproven_correction_link_is_rejected` 里合法的省略回答（"What color bicycle did I buy?"
由 "No, I bought the red one." 回答，两者没有共同区分单元）。新增
`test_elliptical_answer_without_a_clause_marker_stays_sufficient` 把"单子句 + 省略回答仍判完整"钉住。

### 20.4 验证与 pin

- RED→GREEN：4 个新测试先失败（标点容差、改写原因、关系从句收紧、中文逗号收紧）；
- 全量（临时 `masm_test`）：992 passed / 1 skipped（唯一失败是当时过期的 source digest pin）；
- ruff、mypy 全绿；套件（fake）26/26、正负各 13/13、零回退、`planner_failure_reasons={"none":26}`；
- `source_digest` 重新钉为 `2f4adb35df7248d58bafabb9b4f9a425358fd5a7fdb12ba6278ef81e6345d6ac`；
  probe 未改动，`probe_blob`/`probe_sha256` 保持
  `8cff3fbd1af934d1ba36905230f208ce4d7411e4` / `e02b2501ebf415d0a357ddb062785bd229f6874967a9727cc232c247a38f5f02`。

### 20.5 下一步

重建镜像后**再跑一轮诊断**（26 次调用）确认 26/26；只有确认全绿，才值得花那一次五轮资格门槛
（130 次调用）。若诊断轮仍出现 `requirement_quote_reordered` 或 `requirement_quote_paraphrased`，
说明模型不是标点差异而是真的改写问题原文，那时需要的是**契约/提示词层面的明确要求**（逐字复制问题片段），
而不是继续放宽 grounding。

## 21. 第二轮诊断：23/26，两个确定性修复（`dde048b`）

### 21.1 诊断轮二结果

| 用例 | 轮一门槛 | 诊断一 | 诊断二 | 说明 |
| --- | --- | --- | --- | --- |
| `two_attributes` / `cross_source_chain` / `crowded_candidates` / `chinese_context` | 0–3/5 | ✅ | ✅ | R1/R2/R2b 稳定通过 |
| `cross_source_chain_negative` | 0/5 | ❌ | ✅ | 关系从句门控修复 |
| `persisted_two_hop_negative` | 1/5 | ❌ | ✅ | 标点容差 + 无侧车则不强连 |
| `persisted_two_hop`（正例） | 0/5 | ❌ | ❌ | `requirement_count=2`、`support_count=2`、**`link_count=0`**：模型引用了两条事实但从不提 persisted 链接 |
| `missing_value_negative` | 5/5 | ✅ | ❌ | 问题含绑定介词 for，门控当时只认关系代词/逗号，过度声明被放过 |
| `chinese_context_negative` | 0/5 | ❌ | ❌ | 期望 partial 需要模型引用“报名”那条候选；本轮只引用了“里斯本”那条 |

### 21.2 R3：侧车关系改由代码证明

`persisted_two_hop` 的失败不是判定错误，而是**模型没有提出链接**。侧车已存关系（`relation_source_pairs`）
是请求上下文里的事实，与相邻上下文、显式 mention 同类，因此现在由代码直接建 `persisted_relation` 边；
**没有该对时跨来源候选绝不连通**，这一点由新增测试
`test_persisted_relation_edges_require_the_sidecar_pair`（无侧车 → 仍 partial）钉住。

### 21.3 R3b：绑定介词纳入完整性标记

`missing_value_negative` 的问题是 “the storage code **for** the amber key”，唯一候选只说明放在哪个抽屉。
模型对整条候选声明 direct，而问题既无逗号也无关系代词 → 门控沉默 → 判 sufficient 并返回了
“drawer three”。现在门控同时识别**绑定介词短语**（for/of/with/about/regarding）：问题的绑定内容必须被
证据覆盖 → 该用例按期望拒答且证据为空。单子句省略问句（“What color bicycle did I buy?”）不含此类标记，
行为不变。

### 21.4 测试记账（不是放宽断言）

两个探测脚本测试原本用“`missing_value_negative` 对整条候选声明 direct”来**模拟**坏负例；该过度声明
现在已被确定性拒绝，模拟不再产生失败，因此改用 `direct_fact_negative`（同一类过度声明，但问题不含
绑定标记）。**断言完全未变**，包括精确计数与 `insufficient->sufficient` 迁移。

### 21.5 仍未解决的用例与门槛判据问题（需要决策）

`chinese_context_negative` 期望 `partial`，而模型只引用了城市那条候选；要拿到期望的 partial，模型必须
引用“报名”那条。当前门控把这种过度声明变成**安全的拒答**（insufficient + 空证据），因此该用例失败在
安全方向。任何确定性规则都无法既命中期望的 partial、又不误杀
`test_unproven_correction_link_is_rejected` 保护的合法省略回答。

同时记录一个判据层面的发现：planner 请求**已经是 `temperature=0.0`**（`evidence_graph.py`），但
`missing_value_negative` 在两次运行间翻转（上一轮 5/5 通过、本轮失败），说明**波动来自 Provider 侧**。
在“5 轮 × 26 例必须全过”的冻结判据下，即使实现完全正确，单例抖动也会让资格门槛以一定概率失败。
这一点需要在花钱跑五轮之前由用户（或 Codex 评审）决定：接受该判据并按冻结规则承担失败，或事先声明
调整判据（例如要求逐例在 5 轮中表现一致，而非 130 次全绿），二者都必须**在运行前**记录。

### 21.6 验证与 pin

- RED→GREEN：`test_persisted_relation_pair_connects_without_a_planner_link`、
  `test_binding_preposition_question_cannot_be_sufficient_without_its_value` 先失败；
- 全量（临时 `masm_test`）：995 passed / 1 skipped（唯一失败是当时过期的 source digest pin）；
- ruff、mypy 全绿；套件（fake）26/26、正负各 13/13、零回退；
- `source_digest` 重新钉为 `944fae8f4e57ec8046474b7e62f6a6453d9cbb8bf920cb9cae140083c6442a80`；
  probe 未改动（`8cff3fbd…` / `e02b2501…` 不变）。

## 22. 运行前预注册：修正后的资格判据（用户批准，先于运行记录）

### 22.1 为什么需要预注册

第 21.5 节的两个事实：planner 已经是 `temperature=0.0`，但 `missing_value_negative` 在两次运行间翻转
（Provider 侧抖动无法消除）；`chinese_context_negative` 的期望状态取决于模型引用哪一条候选。在冻结的
"5 轮 × 26 例全绿"判据下，**实现完全正确也可能因单例抖动而失败**，而一次失败按规则即淘汰候选。
用户批准改为预注册判据，本节点记录即生效，**在任何五轮运行之前**。

### 22.2 判据全文（`--criterion amended`，默认仍为 `frozen`）

候选合格当且仅当以下五条全部成立：

1. `verification_exit_code == 0`（只读核验：checkout、probe 字节、容器安装摘要、运行档位与 Provider 变量）；
2. `round_count == 5`（固定五轮，不因失败提前退出）；
3. **每一轮**至少 25/26 个用例通过（`positive_passed + negative_passed >= 25`）；
4. **除两个预声明豁免用例外**，每个用例在五轮中至少通过四轮；
5. **每一轮**都不得出现 `forbidden_evidence_selected`（安全硬条件，不豁免）。

预声明豁免（仅豁免第 4 条，第 5 条照旧适用）：

| 豁免用例 | 理由 |
| --- | --- |
| `missing_value_negative` | 期望状态取决于模型是否把"只提及实体"的记忆误判为 direct 覆盖，两次运行间翻转（温度已为 0） |
| `chinese_context_negative` | 期望 `partial` 需要模型引用"报名"那条候选；引用"里斯本"那条时门控会给出**安全的拒答**，失败在安全方向 |

### 22.3 运行方式与固定输出

```bash
python3 scripts/evidence_graph_server_gate.py --container masm-v11-api-1 --stability \
  --criterion amended --strict-exit --output "/tmp/masm-eg-gate-$(date -u +%H%M%S).jsonl"
```

门禁自行计算并只打印固定枚举与计数：`criterion=amended`、一行 `{"case":"amended_criterion",...}`
（`round_count`、`rounds_meeting_minimum`、`cases_total`、`cases_meeting_minimum`、`exempt_cases`、
`forbidden_evidence_total`、`candidate_eligible`）、以及 `amended_candidate_eligible=true|false`。
判据本身由 `_amended_stability_criterion` 实现，其单元测试覆盖四种拒绝路径与一种通过路径。

### 22.4 反钓鱼与不可回退声明

- 本判据在运行前固定；**只对一次五轮运行生效**，不因结果调整；
- 早先 `537c892` 在**冻结判据**下的 0/5 结论不变，仍记录在第 16 节；
- 若修正判据下仍不合格：该候选被淘汰，下一次尝试必须来自**新提交**；
- 修正判据比冻结判据弱，这是用户明确批准的有意放宽，理由与证据（温度已为 0、翻转发生在 Provider 侧）
  记录在第 21.5 节；
- 任何一轮出现 `forbidden_evidence_selected` 即淘汰，安全要求没有被放宽。









