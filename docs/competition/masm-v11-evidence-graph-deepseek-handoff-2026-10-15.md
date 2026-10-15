# MASM v1.1 Evidence Graph — DeepSeek 实现交接

**日期：** 2026-10-15

**用途：** 给 DeepSeek 的独立、可核验实现上下文

**设计规格：** `docs/superpowers/specs/2026-10-15-masm-v11-evidence-graph-search-design.md`

## 1. 任务目标

目标不是继续微调 `evidence-selector-v7`，而是实现一个默认关闭的 query-time grounded
evidence graph，通过“模型提出局部、带原文引用的支持关系 + 确定性校验和组装”解耦：

1. 是否存在直接证据；
2. 跨来源证据是否真正连接；
3. 哪些原始证据应返回；
4. 完整、部分、拒答三态。

官方目标仍是稳定达到 Smoke `45+`，但本地实现和合成门槛不得声称保证分数。DeepSeek
本轮只实现、测试、提交本地代码；不推送、不部署、不运行官方 Smoke 或 Full。

## 2. 当前仓库状态

开始时必须重新核对，不能只相信本文：

| 项目 | 交接时值 |
| --- | --- |
| 工作树 | `C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge` |
| 分支 | `codex/masm-v11-evidence-graph` |
| 设计基线 | `6ad49ad3e99b725821e5e9d1b053d2a0148fe319` |
| 上游实验分支 | `codex/masm-v11-source-context` |
| 线上 API 镜像 | `masm-v11-final-candidate:d74ad1c` |
| 线上 API | `masm-v11-api-1`，最近确认 `running/healthy/restarts=0` |
| 线上数据库 | `masm-v11-postgres-1`，不得重启、重建、迁移或清空 |
| selector prompt | 线上与基线均为 `evidence-selector-v7` |

首先执行：

```powershell
Set-Location 'C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge'
git status --short --branch
git log -8 --oneline --decorate
git diff --check
```

如果工作树不是干净状态，先只读判断改动归属；不得覆盖、stash、删除或回滚不属于本任务
的内容。

## 3. 绝对安全边界

1. 不得读取、显示、复制、移动、删除、暂存或提交
   `E:\Competitions\AgentMemoryChallenge\秘钥.txt`。
2. 不得读取或输出 `.env`、API Key、数据库密码、完整连接串、Provider 请求体/响应体。
3. 不得读取官方隐藏题、隐藏答案、原始生产日志、数据库正文或对象存储内容。
4. 不得访问或修改云服务器、线上容器、线上数据库、域名、Caddy、评测页面。
5. 不得运行 Full；不得运行官方 Smoke；不得用多轮 Provider 调用挑选偶然通过的一轮。
6. 不得触碰 v1.0 路由、容器、卷、代码分支或数据。
7. 不得执行 `git reset --hard`、`git checkout -- <path>`、递归删除或清理用户文件。
8. 测试数据库只能使用独立本机测试实例，数据库名必须确认是 `masm_test`；用完只停止
   本轮启动的测试容器，不能触碰已有业务容器。

## 4. 可确认的评分与实验事实

公开页面曾观察到：

| 状态 | 总分 | direct | atomic | multi-session | abstention |
| --- | ---: | ---: | ---: | ---: | ---: |
| 最好但未证明稳定的一轮 | 38.46 | 100 | 100 | 0 | 22.22 |
| `a7b08fc` partial-abstention | 30.77 | 100 | 100 | 0 | 11.11 |
| 精确 `d74ad1c` | 23.08 | 0 | 100 | 0 | 11.11 |

这些是聚合结果，不能映射到具体隐藏题。已确认的模式：

- atomic retrieval 长期为 `100`，说明写入和基本原子召回并非主要瓶颈；
- multi-session 长期为 `0`；
- direct 与 abstention 会交换或波动；
- 同一 selector 架构在合成 Provider 探针上会出现明显结果波动；
- 本地路径通过不等于官方 multi-session 得分提升。

## 5. 已尝试且不得原样重做的路线

| 路线 | 结果与结论 |
| --- | --- |
| anchor grounding | 伤害 direct recall；不要把 strong anchor 当作答案证据 |
| query decomposition | 未改善付费分数；不能只靠增加查询变体 |
| bounded two-hop persisted relations | 本地链路通过，官方 multi 仍为 0；仅有遍历深度不是根因 |
| semantic expansion | 可改善合成连接链，但拒答不稳定 |
| partial → abstain | 官方得分回落；不能统一清空 partial |
| provenance / blind-origin 多阶段审计 | 输出和判断不稳定，不能再叠加同类自由文本审计 |
| completeness repair | 独立审计把不完整证据判为完整，repair 不触发 |
| generic strong-anchor expansion | 在目标案例 current 已 8/8，无严格提升；strong anchor 不是连接证明 |
| Add-side relation tuning | 本地通过但未改善官方 multi；不能假设写入关系完整 |
| 单纯 source context | 固定套件从 5/10 到 9/10，但跨来源完整性仍失败 |

新架构必须吸收这些结论，而不是换一个 prompt 名称重新实现同一 selector。

## 6. 当前代码结构

重点文件：

| 文件 | 当前职责 |
| --- | --- |
| `src/masm/services/search_service.py` | Search 主流水线、扩展、selector、渲染、诊断 |
| `src/masm/retrieval/evidence_selector.py` | v7 三态整体选择；必须保留为默认和失败回退 |
| `src/masm/retrieval/selector_pool.py` | 32/48 候选的确定性来源多样化 |
| `src/masm/retrieval/relevance.py` | question-derived strong anchor 门控 |
| `src/masm/retrieval/relation_expander.py` | 持久化关系的有界两跳扩展 |
| `src/masm/retrieval/reranker.py` | 排序与 `RankedEvidence` 内部结构 |
| `src/masm/retrieval/source_context.py` | 默认关闭的同 Add 相邻消息上下文恢复 |
| `src/masm/retrieval/evidence_renderer.py` | 返回 selector 实际看到的原始证据/上下文 |
| `src/masm/retrieval/query_analyzer.py` | 简单规则、复杂问题最多三条子查询 |
| `src/masm/retrieval/diagnostics.py` | 内容安全的 Search 聚合遥测 |
| `src/masm/runtime.py` | official/local-fake 组件装配 |
| `src/masm/config.py` | 环境开关与边界验证 |
| `src/masm/storage/repositories.py` | user-scoped 召回、来源、关系读取 |
| `src/masm/storage/types.py` | `MemoryCandidate`、来源快照等领域类型 |

设计基线 `6ad49ad` 相对线上 `d74ad1c` 主要包含：

- 默认关闭的 source-context 实现和测试；
- 只用于实验的 source-context、completeness repair、gate expansion 探针；
- Search 遥测汇总脚本；
- 没有已经通过资格门槛的生产候选。

不要删除这些实验文件；新实现必须通过它们的现有回归测试。

## 7. 实现范围

严格按照设计规格实现，核心范围是：

1. 新建 `src/masm/retrieval/evidence_graph.py`：严格 proposal schema、planner、validator、
   assembler、`GraphEvidenceSelector`。
2. 给 `RelationExpander` 增加带 provenance sidecar 的内部结果，同时保持现有
   `expand()` 行为和顺序不变。
3. 在 Search 内把关系 provenance、来源上下文和候选池传给 graph selector；不得改变
   公共 Add/Search schema。
4. 新增默认关闭的 `MASM_EVIDENCE_GRAPH_ENABLED` 及有硬上限的配置。
5. 在 `runtime.py` 和 app 装配中按开关选择 graph selector；默认路径仍是 v7。
6. 扩展安全诊断，只记录固定枚举和计数。
7. 新增合成探针和测试，但不得调用官方评测或读取隐藏数据。

允许为接口清晰增加很小的内部 protocol/dataclass。不要在
`search_service.py` 中堆积 planner、验证器和图算法。

## 8. 必须保持不变的契约

- `EvidenceSelector`、`EvidenceSelection`、`PROMPT_VERSION="evidence-selector-v7"` 的既有
  语义和测试。
- public `/add`、`/search`、健康检查、响应模型和认证。
- `top_k <= 100`、响应字节上限、最多 16 并发等现有配置边界。
- 原始文本、多模态资产和来源顺序；不得把模型生成摘要作为公开证据。
- 所有 SQL 层 user isolation。
- 删除一个 evaluation run 时 context/message/source/relation/assets 的完整清理。
- local-fake 档位不得产生网络请求。
- feature flag 关闭时，相同输入必须走原 v7 路径，且新增 planner 请求数为 0。

## 9. 工作方法

DeepSeek 开始实现前必须先输出一份逐任务计划并等待用户确认。计划需要包含：

1. 先写的失败测试及预期 RED 原因；
2. 每个新内部接口；
3. 哪些旧文件只读、哪些允许修改；
4. 单元、集成、全量和静态检查命令；
5. 每个提交的边界和提交信息；
6. 如何证明默认关闭、v7 无回归、无迁移、无内容泄漏。

实现时一项一项完成，不能同时修改多个假设。任何测试意外通过或失败原因与预期不符，
先停下定位根因，不要为了变绿修改断言。

## 10. 测试与完成定义

最低完成条件：

- 新图组件的目标测试先 RED 后 GREEN；
- 设计规格第 11 节所有必需案例有明确覆盖；
- 既有 selector/source-context/relation/retrieval/deletion/isolation/API tests 通过；
- 完整 `pytest -q` 通过；
- `python -m ruff check .` 通过；
- `python -m mypy` 覆盖 `src`、新脚本及既有相关脚本并通过；
- `git diff --check` 通过；
- feature flag 默认关闭测试通过；
- 对基线的 diff 没有数据库 migration、公共 schema、v1.0 或部署文件的无关变化；
- 本地临时数据库/容器停止，且合成探针零残留；
- 工作内容被拆成可审查的小提交；
- 最终交接记录实际命令、退出码、passed/skipped/warning 数，不能复用旧数字。

Provider 稳定性门槛、16/16 并发、服务器构建、API-only canary 和官方 Smoke 不属于
DeepSeek 这轮实现范围。可以准备脚本和运行手册，但不能自行执行。

## 11. 建议提交顺序

这不是可以跳过用户计划确认的现成实施授权，仅用于约束提交边界：

1. `test: specify grounded evidence graph behavior`
2. `feat: add grounded evidence graph planner and validator`
3. `feat: assemble evidence from validated graph paths`
4. `feat: wire opt-in evidence graph search`
5. `test: add evidence graph integration and safe probe`
6. `docs: hand off evidence graph candidate`

每个行为提交都必须包含对应测试；不要把全部改动压成一个提交。

## 12. 交付给 Codex 的证据

实现完成后，DeepSeek 最终回复必须包含：

- 当前绝对工作树路径、分支、完整 HEAD；
- `git status --short --branch`；
- 从本文档提交到 HEAD 的提交列表；
- `git diff --stat` 和所有修改文件列表；
- 失败先行测试的 RED 命令和失败原因；
- GREEN 的 focused/full/Ruff/mypy/diff-check 命令与实际摘要；
- 是否启动过测试数据库/容器，以及最终是否停止；
- 未运行 Provider、服务器部署、Smoke、Full 的明确声明；
- 已知限制、尚未满足的门槛和建议下一步；
- 不包含任何秘密、正文、原始日志或隐藏评测信息。

Codex 新对话会独立核对，不会仅凭交接文字宣称完成。
