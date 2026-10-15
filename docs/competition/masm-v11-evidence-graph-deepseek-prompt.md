# 给 DeepSeek 的实现 Prompt

把下面整段作为 DeepSeek 新会话的第一条消息。DeepSeek 必须先给出实施计划并等待你的
确认，不能收到本 Prompt 后直接改代码。

---

你将接手 AgentMemoryChallenge 的 MASM v1.1 evidence graph 架构实现。

工作目录固定为：

`C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge`

预期分支：`codex/masm-v11-evidence-graph`。

先只读执行并报告：

```powershell
Set-Location 'C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge'
git status --short --branch
git log -8 --oneline --decorate
git diff --check
```

然后完整阅读：

1. `docs/superpowers/specs/2026-10-15-masm-v11-evidence-graph-search-design.md`
2. `docs/competition/masm-v11-evidence-graph-deepseek-handoff-2026-10-15.md`
3. `docs/competition/masm-v11-source-context-experiment.md`
4. `src/masm/services/search_service.py`
5. `src/masm/retrieval/evidence_selector.py`
6. `src/masm/retrieval/relation_expander.py`
7. `src/masm/retrieval/source_context.py`
8. `src/masm/retrieval/selector_pool.py`
9. `src/masm/retrieval/relevance.py`
10. `src/masm/retrieval/reranker.py`
11. `src/masm/retrieval/diagnostics.py`
12. `src/masm/runtime.py`、`src/masm/config.py` 和相关测试。

目标：实现默认关闭的 query-time grounded evidence graph。模型只提出问题需求、候选原文
引用和候选间链接；确定性代码负责 quote grounding、真实边验证、连通性、完整/部分/不足
三态和最终证据组装。保留 `evidence-selector-v7` 为默认路径，并仅在 graph planner
transport/schema/结构失败时调用一次作为回退。不得把 strong anchor、相似度、排名接近或
共享泛化主题当作连接证明。

关键实现要求：

- 新图逻辑放在聚焦模块中，不要堆进 `search_service.py`。
- proposal 使用严格 Pydantic schema，不允许最终答案、自由文本 rationale 或模型直接给出
  `selected_indices`/全局 `evidence_state`。
- question quote 和 candidate quote 必须能在 planner 实际看到的文本中确定性匹配。
- 只接受三类可证明连接：同来源相邻上下文、relation expansion provenance sidecar 中的
  持久化边、满足规格中 discriminative-unit 规则的显式跨来源 mention。
- `RelationExpander.expand()` 的既有返回、顺序、上限和测试保持不变；用 additive internal
  API 提供 sidecar。
- graph mode 有独立环境开关且默认 `0`；配置冲突必须启动时拒绝，不能静默打开依赖。
- graph mode 关闭时 planner 调用数必须为 0，行为回到冻结的 v7 selector。
- 不修改 public Add/Search schema，不做数据库 migration，不改 v1.0。
- 所有内部数据和 SQL 读取都按 `user_id` 隔离。
- 诊断只记录固定枚举、计数和时延，不记录问题、候选、quote、ID、模型输出或秘密。
- 不要改弱已有测试，不要通过重跑选择偶然成功结果。

工作方式：

1. 在修改任何文件前，先输出一份逐任务实施计划，包括：失败测试、接口、文件范围、每次
   提交边界、验证命令和回滚方式。
2. 等我明确回复批准计划后，才开始编辑。
3. 严格 TDD：先提交能在冻结基线上证明缺口的 RED 测试，再写最小实现变 GREEN。
4. 一次只验证一个架构假设；意外失败先找根因，不要堆补丁。
5. 按 handoff 建议拆成多个可审查提交。

必须覆盖的案例以设计规格第 11 节为准，包括 direct、双属性、跨来源链、省略确认、纠正、
持久化两跳、错误主体、断链、缺失值、option 干扰、strong-anchor 干扰、拥挤候选、冲突、
重复来源、中文、非法 schema/quote/index、planner 回退、valid-insufficient 不回退、纯图片
不回归以及遥测不泄漏。还要有真实 repository/Search 集成测试验证来源渲染一致和跨用户边
被拒绝。

完成前运行并记录实际结果：

```powershell
# 使用仓库现有虚拟环境；先确认实际 Python 路径，再运行
python -m pytest <新增和相关 focused tests> -q
python -m pytest -q
python -m ruff check .
python -m mypy src <新增脚本和既有相关脚本>
git diff --check
git status --short --branch
git log --oneline --decorate <设计文档提交>..HEAD
```

不要伪造或复用旧测试数字。若启动本机临时数据库，必须确认数据库名严格为 `masm_test`，
记录容器名，并在结束时只停止本轮启动的测试容器。

绝对禁止：

- 读取、显示、复制、移动、删除、暂存或提交
  `E:\Competitions\AgentMemoryChallenge\秘钥.txt`；
- 读取或输出 `.env`、API Key、数据库密码、完整连接串、原始 Provider body；
- 读取隐藏评测内容、原始线上日志或生产数据库正文；
- 访问或修改华为云服务器、线上容器、线上数据库、域名或评测页面；
- push、部署、运行官方 Smoke 或 Full；
- `git reset --hard`、覆盖用户改动、递归删除工作区；
- 通过修改预期断言、放宽安全门槛或重复 Provider 运行让结果“看起来通过”。

本轮交付是：本地实现提交、测试证据和更新后的本地交接文档。最终回复必须给出绝对路径、
分支、完整 HEAD、提交列表、diff 文件列表、RED/GREEN 证据、全量与静态检查摘要、临时容器
最终状态、未运行部署/评测的声明、已知限制和下一步。不要输出任何秘密或原始内容。

---
