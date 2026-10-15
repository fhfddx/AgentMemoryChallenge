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

   服务器上应使用既有秘密注入机制，不要把值写入命令、仓库或回传日志。执行前只核对下列变量已由运行环境提供：
   `MASM_RUNTIME_PROFILE=official-masm`、`MASM_LLM_BASE_URL`、`MASM_LLM_API_KEY`、
   `MASM_EMBEDDING_BASE_URL`、`MASM_EMBEDDING_API_KEY`。建议命令：

   ```bash
   if (
     test "${MASM_RUNTIME_PROFILE:-}" = "official-masm" || {
       printf '%s\n' 'MASM_RUNTIME_PROFILE is not official-masm' >&2
       exit 2
     }
     for name in \
       MASM_LLM_BASE_URL \
       MASM_LLM_API_KEY \
       MASM_EMBEDDING_BASE_URL \
       MASM_EMBEDDING_API_KEY
     do
       test -n "${!name:-}" || {
         printf 'missing required variable: %s\n' "$name" >&2
         exit 2
       }
     done

     set -o pipefail
     .venv/bin/python scripts/evidence_graph_probe.py --stability \
       | tee /tmp/masm-evidence-graph-stability.jsonl
     status=${PIPESTATUS[0]}
     tail -n 1 /tmp/masm-evidence-graph-stability.jsonl
     exit "$status"
   ); then
     probe_status=0
   else
     probe_status=$?
   fi
   printf 'probe_exit_code=%s\n' "$probe_status"
   ```

   外层 `if` 在交互式 shell 中接住子 shell 的非零状态；子 shell 内的 `exit` 不会结束 SSH 会话，即使外层开启了 `set -e`。只需回传 `probe_exit_code`
   和最后一行 `stability_summary`；不要回传环境变量、Provider 响应或其他服务日志。
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

代码/测试 HEAD（本次最终文档提交之前）：`0348d78`。最终文档提交和完整 HEAD 以交付消息中的实际
`git log` 为准。

最后一轮是主代理自审（本会话禁止未经用户要求的子代理委派），因此不把它等同于独立第二审。自审发现的两个 P2 均已通过先 RED 后 GREEN 的回归测试处理。

### 14.3 最新资格判断

本地 deterministic、数据库 integration、隔离、契约、Ruff、mypy 与 diff-check 门槛全部通过，
因此该分支可作为“进入下一阶段 Provider 稳定性门槛”的候选。`--stability` 已把五轮固定门槛收敛为单进程、非零失败的可执行命令；它仍未通过真实 Provider 五轮固定
套件、API-only canary 或官方 Smoke；不得据此宣称官方分数提高，也不得直接部署。
