# DeepSeek 完成后，给 Codex 新对话的 Prompt

DeepSeek 完成本地实现后，把下面整段作为 Codex 新对话的第一条消息，并在末尾粘贴
DeepSeek 的最终交接摘要。不要粘贴密钥、`.env`、原始日志、候选正文或隐藏评测内容。

---

请接手并独立审查 AgentMemoryChallenge 的 MASM v1.1 grounded evidence graph 实现。
DeepSeek 已经写完代码，但不要相信“已完成”结论；先核对实际 Git 状态、diff、测试和运行
边界，再决定是否需要修复。目标仍是形成一个有资格进入 API-only canary 的候选，不是直接
部署或运行评测。

工作目录：

`C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge`

预期分支：`codex/masm-v11-evidence-graph`。

必须先完整阅读：

1. `docs/superpowers/specs/2026-10-15-masm-v11-evidence-graph-search-design.md`
2. `docs/competition/masm-v11-evidence-graph-deepseek-handoff-2026-10-15.md`
3. DeepSeek 更新后的交接/验证文档和本消息末尾的摘要。

先只读执行：

```powershell
Set-Location 'C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge'
git status --short --branch
git log --oneline --decorate -20
git diff --stat 6ad49ad3e99b725821e5e9d1b053d2a0148fe319..HEAD
git diff --name-status 6ad49ad3e99b725821e5e9d1b053d2a0148fe319..HEAD
git diff --check 6ad49ad3e99b725821e5e9d1b053d2a0148fe319..HEAD
```

审查要求：

- 按严重度优先报告具体发现，必须附绝对文件路径和行号；没有问题时明确说没有发现，并
  列出仍未验证的风险。
- 对照设计逐项核查，不接受只看测试是否绿色。
- 特别检查 planner 是否偷偷返回答案、自由 rationale、selected indices 或全局状态；
  validator 是否真的验证 quote、user scope 和边来源；assembler 是否由有效图确定性导出
  complete/partial/insufficient；valid-insufficient 是否错误回退；planner 失败是否只回退
  一次到冻结 v7。
- 检查 `RelationExpander` provenance sidecar 是否 additive，旧 `expand()` 顺序、上限、冲突
  优先级、环/diamond 去重与 SQL user isolation 是否保持。
- 检查 graph flag 是否默认关闭，关闭时 planner 调用数为 0，配置组合是否启动时验证。
- 检查 public Add/Search schema、数据库 schema、删除、source rendering、多模态、响应预算、
  local-fake 和 16 并发边界是否被意外改变。
- 检查 diagnostics、异常处理和测试失败输出是否泄漏问题、候选、quotes、IDs、Provider body
  或秘密。
- 检查测试是否真正先固定行为，是否存在过拟合 fixture、仅 mock 自己实现、放宽断言、跳过
  或重复运行挑结果。
- 核对 DeepSeek 声称的每个命令；必要时从头重跑 focused/full/Ruff/mypy/diff-check。

如果发现缺陷：先解释根因和影响，再用 TDD 做最小修复；不要顺手重构，也不要把多项假设
混在一个提交。修复后重新运行受影响测试和全量门槛。若实现偏离规格到需要改变架构，停止
编码，先向我说明并给出设计修订建议。

安全边界：

- 不得读取、显示、复制、移动、删除、暂存或提交
  `E:\Competitions\AgentMemoryChallenge\秘钥.txt`。
- 不得读取/输出 `.env`、API Key、数据库密码、完整连接串、原始日志、生产数据库正文、
  Provider body 或隐藏评测内容。
- 不得访问或修改服务器、线上 API/DB、域名、评测页面；不得 push、部署、运行 Smoke/Full。
- 不得 `git reset --hard`、覆盖/清理不属于本任务的文件。
- 如需数据库测试，只能使用已核验的本机 `masm_test`，并只停止本轮启动的测试容器。

最终交付应包括：审查发现及处理、实际 HEAD/提交列表、修改文件、验证命令和最新输出摘要、
工作树状态、临时容器状态、未运行 push/部署/评测的声明、是否满足“可以进入下一阶段
Provider 稳定性门槛”的判断。即使本地全部通过，也不要宣称官方分数会提高。

DeepSeek 最终交接摘要粘贴在这里：

`<只粘贴脱敏摘要，不要粘贴秘密或原始内容>`

---
