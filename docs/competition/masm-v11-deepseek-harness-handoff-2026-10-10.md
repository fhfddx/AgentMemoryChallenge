# MASM v1.1 DeepSeek Harness 交接（2026-10-10）

## 0. 更新：v7 部署与官方 Smoke（`84926f6`）

> **本节取代后文所有旧的“当前状态”描述。** `84926f6` 已部署到 v1.1，官方 Smoke 和
> 脱敏遥测比较均已完成；历史章节仍保留实验过程。第 2 节的边界继续有效。

### 0.1 当前代码状态

| 项目 | 当前值 |
| --- | --- |
| 工作树 | `E:\Competitions\AgentMemoryChallenge\.worktrees\masm-v11` |
| 分支 | `codex/masm-v11-selector-probe` |
| 部署来源提交 | `84926f6`（部署时远端分支顶端；该提交只改文档） |
| 最新代码提交 | `acc5b8a`（最后一个改动 `src/` 或 `scripts/` 的提交） |
| 部署时远端 | `origin/codex/masm-v11-selector-probe` = `84926f6` |
| 本次文档更新 | 提交 SHA 不在本文内固定；提交后需由用户手动推送 |
| selector prompt version | `evidence-selector-v7` |
| selector 决策协议 | 三态 `evidence_state ∈ {sufficient, partial, insufficient}` |
| 线上镜像 / 提交 | `masm-v11-final-candidate:84926f6` |
| 官方 Smoke | `teval_9ec9bed551755c3c`，成功，总分 `30.77` |

`b8c1920..84926f6` 共有 6 个提交。其中 4 个改动代码，2 个只改文档；本次追加的
Smoke 结果记录是另一个文档提交，其 SHA 不写入本文，以免形成自引用。

| 提交 | 作用 |
| --- | --- |
| `0292f3b` | 二态 `sufficient_evidence` 换成三态 `evidence_state`：`partial` 返回已验证索引且不回落强锚点，`insufficient` 一律拒答；旧二态 Provider 输出经 `model_validator(mode="before")` 映射（`false → insufficient`），旧字段既不进 schema 也不发给模型；遥测新增固定枚举 `selector_evidence_state` |
| `0e783f1` | v6 收窄 `partial`：必须能指名一条被选记忆逐字陈述的被询问事实，不得因「不确定」或「看起来相关」而报 partial；关系型问题在其记忆覆盖全部事实与链路时允许 `sufficient` |
| `eb33e6e` | v7 把 `evidence_state` 放到 schema 属性首位。严格解码按属性顺序生成，原先 `selected_indices` 在前，模型在判定「不足」之前就已写好索引；调序后 `insufficient` + 非空 indices 的矛盾消失 |
| `acc5b8a` | 首轮 `partial` 也用更深候选池（32→48）重试；深层轮只在给出模型确认的完整 selection 时才替换首轮，否则原样保留首轮 partial。`selector_reasoning_probe.py` 增加 `production_eligible` 与 `--strict-only` 生产门槛，`candidate_eligible` 降为记录项 |
| `1fb43fc` | 只更新本文档，不改源码或测试 |
| `84926f6` | 修正交接元数据与审计证据，只改本文档；随后用于服务器构建镜像标签 |

本地门槛在最新代码提交 `acc5b8a` 上测得：`pytest -q` → `773 passed, 1 skipped`；
Ruff 通过；mypy 通过（60 个源文件）；`git diff --check` 通过。

### 0.2 合成探针结果（脱敏，仅枚举与计数）

门槛在开跑之前写死。预注册的是**各案例的期望值**，它们自 `0292f3b` 起没有被改动：

- `scripts/selector_probe.py` 自 `0292f3b` 起没有任何差异；
- `scripts/selector_reasoning_probe.py` 在 `acc5b8a` 中被修改过（`run_probe` 的生产门槛、
  `main` / `--strict-only` CLI、`_report_failure` 输出，以及 `argparse` 导入），
  但 `_cases()` 里每个案例的 `expected` 索引集合与 `expected_state` 均未改动。

因此不要再用「整个脚本无差异」来证明门槛未被移动；能被证明的只是**期望值未变**。
判据本身是：

- Gate 1 `selector_probe.py` 默认 4 用例：4/4 × 连续 3 轮；
- Gate 2 `selector_probe.py --crowded-only` 3 用例：3/3 × 5 轮；
- Gate 3 `selector_reasoning_probe.py` 生产 prompt：4/4 × 连续 2 轮。

| 版本 | Gate 1 | Gate 2 | Gate 3 |
| --- | --- | --- | --- |
| v5 | 4/4 | **`crowded_abstention` 误报 partial，5/5 轮失败** | **strict 2/4**（`chain_missing_link` 与 `chain_complete` 均报 partial） |
| v6 | 4/4（第 1 轮 1 次 provider `unavailable`） | 15/15 通过 | **strict 3/4**（`insufficient` + 非空 indices） |
| v7 | **连续 5 轮 4/4 通过** | **15/15 通过** | **连续 3 轮 4/4 通过** |
| v7 部署后（`84926f6`） | **3 轮共 12/12 通过** | **5 轮共 15/15 通过** | **2 轮共 8/8 通过，`production_eligible=true`** |

v7 三条门槛均按字面达成。`candidate_eligible` 仍为 `False`，因为生产 prompt 已覆盖
chain 变体的用例；它表示「chain 优于 strict」，**不是**部署门槛。`chain` prompt 只作对照
基线，永远不是部署候选。

### 0.3 仍未闭环的风险

**Provider `unavailable` 会退回全量 question-admitted 强锚点。** 约 220 次部署前合成调用中观察到
3 次 `selector_failure_category=unavailable`，每次结果都是 `selector_evidence_state=unknown`、
`selector_fallback=true`、返回最多 12 条强锚点。这是 `0fc322d` 已有的 v4 行为，与三态改动
无关，但会让一个本该拒答的问题带着未经模型确认的证据被作答，直接影响 abstention 分项。
`84926f6` 的 14 行官方 Smoke 遥测中 `fallback=0` 且失败类别全为 `none`，所以该风险没有在
本次官方运行中触发，也不能解释本次总分。风险仍存在，但不应与 multi-session 修复混在同一候选。

### 0.4 当前结论与未完成项

1. API-only 部署已完成：API 容器已替换，DB 容器 ID 未变，API 为 `running/healthy`、
   `restarts=0`，容器内 prompt 为 `evidence-selector-v7`。
2. 官方 Smoke 总分仍为 `30.77`：direct recall 恢复到 `100`，abstention 从 `22.22`
   回落到 `11.11`，属于能力交换而不是总分提升。
3. 线上暂时保留 `84926f6`；它比 `0fc322d` 更均衡，但不是已确认的最终最优版。
4. 不再为 selector 运行第二次付费 Smoke。下一独立候选应冻结 selector，聚焦始终为 `0` 的
   multi-session reasoning。

## 1. 文档用途与当前结论

本文是给后续 DeepSeek Harness 会话的独立交接快照。它聚焦 2026-10-10
围绕 evidence selector 的实验、云端部署和官方 Smoke 结果，不替代仓库中更早的
总交接、发布清单和云端恢复报告。

当前线上候选为 `84926f6`，服务健康且数据库容器未被替换，但它不是已确认的最终最优版：

- 官方 Smoke 总分仍为 `30.77`；
- direct recall 从 `0.00` 恢复到 `100.00`；
- abstention 从 `22.22` 回落到 `11.11`，但没有归零；
- multi-session reasoning 仍为 `0.00`。

因此，三态 selector 实验到此冻结。下一步的核心不是继续扩大候选池或微调 selector prompt，
而是用独立候选调查 multi-session reasoning 的数据流、检索链路和回答阶段；不得把 selector
改动混入同一实验。

## 2. 必须遵守的边界

1. 不读取、保存、输出或推断官方隐藏问题、隐藏答案、原始用户内容或对象 URI。
2. 只使用公开评分、合成探针和已脱敏的聚合遥测字段做判断。
3. 不把原始容器日志、请求体、数据库行或 Provider 凭据复制给模型。
4. 不记录或提交 API Key、数据库密码、完整连接串及任何 `.env` 内容。
5. 云端只允许替换 `masm-v11-api-1`；不得重建、重启或清空
   `masm-v11-postgres-1`。
6. 不触碰 v1.0 容器、卷、数据或公网路由。
7. 没有失败先行测试、完整验证和回滚点时，不部署新候选。
8. 一个 Smoke 的分项变化只能作为聚合证据，不能映射到某条隐藏样本。
9. 不要仅凭总分不变宣称回归已解决；必须同时检查各分项。
10. Full 评测成本和风险更高，当前阶段不要启动 Full。

## 3. 仓库、分支与当前提交

| 项目 | 当前值 |
| --- | --- |
| 仓库 | `https://github.com/fhfddx/AgentMemoryChallenge.git` |
| Windows 主仓库 | `E:\Competitions\AgentMemoryChallenge` |
| 当前工作树 | `E:\Competitions\AgentMemoryChallenge\.worktrees\masm-v11` |
| 分支 | `codex/masm-v11-selector-probe` |
| 部署来源提交 | `84926f6` |
| 最新代码提交 | `acc5b8a` |
| 远端状态（本次更新前） | `origin/codex/masm-v11-selector-probe` = `84926f6`；本次文档提交待用户手动推送 |
| 服务器检出目录 | `/opt/probe-29a3a13` |

服务器目录名保留了旧提交号，但目录内 Git HEAD 已前进到当前提交。不要根据目录名判断
实际版本，必须使用 `git -C /opt/probe-29a3a13 rev-parse --short=7 HEAD`。

当前相关提交从新到旧如下：

| 提交 | 作用 |
| --- | --- |
| `84926f6` | 修正交接元数据；服务器以此提交构建当前镜像 |
| `1fb43fc` | 记录 v5–v7 selector 实验和部署前状态 |
| `acc5b8a` | partial 深层重试与 production prompt 门禁 |
| `eb33e6e` | v7 调整严格 schema 字段顺序 |
| `0e783f1` | v6 收窄 partial 的直接事实判据 |
| `0292f3b` | 引入三态 evidence protocol |
| `0fc322d` | 对 `sufficient_evidence=false` 且 indices 非空的矛盾输出执行安全拒答 |
| `a814349` | 在合成 reasoning probe 中细分安全的失败原因 |
| `29a3a13` | 新增 strict 与 chain-aware selector prompt 对照探针 |
| `76c7595` | strict selector 使用更深的候选池重试 |
| `dda1222` | 在 selector 回退中保留部分直接证据 |
| `764d1c6` | 新增只输出安全字段的 Search 诊断比较脚本 |
| `2beea0b` | 稳定 evidence sufficiency selection |
| `e2ad957` | 新增脱敏 Search 诊断遥测 |

DeepSeek Harness 开始工作时先执行：

```powershell
Set-Location 'E:\Competitions\AgentMemoryChallenge\.worktrees\masm-v11'
git status --short --branch
git log -8 --oneline --decorate
git diff --check
```

如果工作树不干净，先判断改动来源；不要覆盖、stash 或删除不属于本次任务的文件。

## 4. 当前线上部署

| 项目 | 当前值 |
| --- | --- |
| 公网端点 | `https://v11.agentmemorydev.icu` |
| Compose project | `masm-v11` |
| API 容器 | `masm-v11-api-1` |
| PostgreSQL 容器 | `masm-v11-postgres-1` |
| 当前镜像 | `masm-v11-final-candidate:84926f6` |
| 当前 override | `/opt/masm-v11/config/image.override.84926f6.yml` |
| 环境文件 | `/opt/masm-v11/config/v11.env` |
| Compose 文件 | `/opt/probe-29a3a13/deployments/docker-compose.v11.yml` |
| selector prompt version | `evidence-selector-v7` |

最近一次部署后的已验证状态：

- API 容器 ID 已变化，说明 API 被替换；
- PostgreSQL 容器 ID 未变化；
- API 为 `running`、`healthy`、`restarts=0`；
- 容器内 `PROMPT_VERSION` 为 `evidence-selector-v7`。

Compose 曾先提示无法从远端拉取本地镜像名，随后按 `build` 配置在服务器本地成功构建，
最终显示镜像 `Built`、API `Started`。单独的 pull 警告不是失败；必须以最终容器镜像、
健康状态和容器 ID 校验为准。

## 5. 官方 Smoke 评分时间线

以下数字只来自用户可见的官方评分页面，不包含隐藏题内容。

| 候选 | 运行 | 总分 | 检索 | direct recall | atomic retrieval | multi-session reasoning | abstention |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `dda1222` | `teval_972233aa176a075d` | 23.08 | 66.67 | 0.00 | 100.00 | 0.00 | 11.11 |
| `76c7595` 阶段 | `teval_da8097e4c5702139` | 30.77 | 100.00 | 100.00 | 100.00 | 0.00 | 11.11 |
| `0fc322d` | `teval_9a04b8a94e48a212` | 30.77 | 66.67 | 0.00 | 100.00 | 0.00 | 22.22 |
| `84926f6` | `teval_9ec9bed551755c3c` | 30.77 | 100.00 | 100.00 | 100.00 | 0.00 | 11.11 |

最新 `84926f6` Smoke 的页面状态为成功，耗时 `7m46s`，完成时间为
`2026-10-10 17:57:46`。只运行了这一轮，没有启动第二次 Smoke。

可以从聚合分数得出的结论：

- `0fc322d` 的保守规范化提高 abstention，但损失 direct recall；
- `84926f6` 的三态协议恢复 direct recall，同时把 abstention 换回 `11.11`；
- `84926f6` 与 `76c7595` 的公开聚合分项完全相同，总分没有提高；
- atomic retrieval 保持为 100；
- multi-session reasoning 尚未取得得分。

不能从这些分数得出的结论：

- 不能断言某个遥测行就是 direct recall 或 abstention 的具体隐藏题；
- 不能断言最新版本在所有拒答样本上更安全；
- 不能把 `30.77` 当成发布门槛已通过。

## 6. 当前代码行为

核心文件：`src/masm/retrieval/evidence_selector.py`。

当前 `EvidenceSelector.select()` 对结构化输出的处理顺序是：

1. Provider 或结构化解析异常：按异常类型标记 `unavailable` 或
   `invalid_output`，再走 strong-anchor fallback。
2. `evidence_state=insufficient`：始终返回空证据并拒答；若 indices 非空，同时标记
   `invalid_output`，但不回落到强锚点。
3. `evidence_state=partial`：只有 indices 合法且非空时返回这部分直接证据；否则安全拒答并
   标记 `invalid_output`。
4. `evidence_state=sufficient`：合法时返回完整 selection；indices 非法时走强锚点 fallback。
5. 首轮为 `partial` 或拒答且存在更多排名候选时，从 32 扩到 48 重试；首轮 partial 只有在
   深层轮得到模型确认的完整 selection 时才被替换，否则保留首轮 partial。

旧 Provider 的 `sufficient_evidence` 布尔字段仍由输入 validator 兼容：`true → sufficient`、
`false → insufficient`；旧字段不会进入发给模型的严格 schema。当前 prompt 版本为
`evidence-selector-v7`，并要求模型先生成 `evidence_state` 再生成 indices。

对应失败先行回归测试位于：

```text
tests/unit/retrieval/test_evidence_selector.py
test_inconsistent_insufficient_decision_normalizes_to_safe_abstention
```

最新代码提交 `acc5b8a` 已完成以下验证：

- 完整测试：`773 passed, 1 skipped`；
- Ruff 通过；
- mypy 通过，共检查 60 个源文件；
- `git diff --check` 通过。

这些结果证明当前断言和静态检查通过，不证明官方效果最优。

## 7. 已验证的实验结论

### 7.1 扩大候选池不是根因修复

`76c7595` 增加了 strict selector 的更深候选池重试。脱敏比较中 14 行里有 11 行发生变化，
但官方总分仍为 `30.77`。不要继续仅靠把 32 改成 48 或更大来期待解决核心问题。

### 7.2 chain-aware prompt 不具备部署资格

`scripts/selector_reasoning_probe.py` 用 4 个完全合成的公开案例比较 strict 和 chain prompt，
每轮共 8 次模型调用，只输出计数和分类，不输出正式评测内容。

重复探针显示：

- chain prompt 在 missing-link 案例上仍会产生
  `sufficient_evidence=false + selected_indices 非空`；
- 某轮 strict 为 4/4、chain 为 3/4；
- 后续诊断轮 strict 为 2/4、chain 为 3/4；
- 探针最终输出 `candidate_eligible=false`。

这同时说明：

- chain prompt 没达到“全用例通过且严格优于 strict”的门槛；
- 即使 `temperature=0`，结构化决策仍存在运行间波动；
- 不能把一次合成探针的较好结果直接当成部署依据。

### 7.3 跨字段矛盾是稳定出现的协议问题

输出 schema 可以分别约束布尔值和 index 数组，但不能仅靠现有 JSON schema 表达
“`sufficient_evidence=false` 时 indices 必须为空”这一跨字段条件。代码能够检测矛盾，
但目前只能在“保留部分证据”和“全部拒答”之间二选一，而官方分数表明两端都有代价。

### 7.4 v4 crowded 合成探针通过，但覆盖不足

已有 crowded-only 合成探针通过以下案例：

- `crowded_abstention`；
- `crowded_multi_source`；
- `deep_crowded_multi_source`。

这证明 v4 在这些合成边界上可工作，不代表它覆盖了 official direct recall 与 abstention
之间的冲突。

## 8. `84926f6` 安全遥测比较

本次 Smoke 生成并比较了以下文件：

```text
/tmp/smoke-84926f6-all.log
/tmp/smoke-84926f6-search-diagnostics.jsonl
/tmp/smoke-v2-search-diagnostics.jsonl
/tmp/smoke-v2-84926f6-compare.txt
```

启动前的时间标记文件没有落地，因此没有伪造新的 marker；而是根据用户可见的完成时间
`2026-10-10 17:57:46`（Asia/Shanghai）和耗时 `7m46s`，使用保守 UTC 窗口
`2026-10-10T09:49:00Z..09:59:00Z` 提取日志。最终 `search.completed` 文件恰为 14 行。

只读取了 `scripts/compare_search_diagnostics.py` 的安全输出，没有把原始日志或 JSONL 内容复制
到 Harness 对话。聚合结果如下：

| 指标 | v2 baseline | `84926f6` |
| --- | ---: | ---: |
| rows | 14 | 14 |
| candidate_zero | 3 | 3 |
| returned_zero | 9 | 7 |
| abstained | 8 | 6 |
| fallback | 0 | 0 |
| failures | `none: 14` | `none: 14` |

`84926f6` 的三态分布为 `insufficient=4`、`partial=1`、`sufficient=6`、`unknown=3`；
baseline 不含该字段，显示为 `missing=14`。比较脚本报告 `CHANGED_ROWS` 为 1–14 全部行。

这些数据只说明新版本少了 2 个空返回和 2 次拒答，且本轮没有 fallback 或 Provider/解析失败；
它与官方页面中 direct recall 恢复、abstention 回落的方向一致。不能据此把某行映射到具体隐藏题，
也不能因为 14 行全部变化而推断 14 个隐藏题的语义都改变。

## 9. 推荐给 DeepSeek Harness 的下一步

### 阶段 A：只读复核，不改代码

1. 核对 Git 分支、HEAD 和工作树是否干净。
2. 把 `84926f6` 视为 selector 冻结基线；不要同时修改 selector prompt、候选池大小或 fallback。
3. 追踪 multi-session 数据从写入、检索、关联到回答生成的完整路径，优先阅读
   `search_service.py`、memory/retrieval 相关模块及其现有测试。
4. 明确列出哪些判断是代码事实、公开合成测试事实、官方聚合事实或推断；不得反推隐藏题。

### 阶段 B：先建立新的合成失败用例

在实现前，至少增加两个完全公开、彼此对照的 multi-session 场景：

1. **跨会话可连接**：事实分别位于不同会话，但包含明确、确定性的共同实体或链路；目标是
   证明系统能够检索并组合必要证据。
2. **跨会话不可连接**：表面主题相似但缺少关键实体或关系；目标是防止为了提高 reasoning
   而错误拼接无关记忆。

测试必须先在冻结的 `84926f6` 代码上至少有一个失败，再实现。不要使用或仿造隐藏题。

### 阶段 C：一次只修改 multi-session 链路

先定位失败发生在候选生成、跨会话关联、证据选择还是最终回答阶段，再选择最小修改点。
新候选不得改动 v7 selector 的 prompt/schema/三态语义，除非独立证据证明 selector 正是
multi-session 的阻断点；即使如此，也应拆成后续单独实验。新增遥测只能记录枚举、计数和
布尔值，不能记录查询、记忆或回答内容。

### 阶段 D：本地门槛

每个实现至少依次通过：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/retrieval/test_evidence_selector.py tests/unit/test_selector_reasoning_probe_script.py tests/unit/test_compare_search_diagnostics_script.py -q
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m mypy src scripts/selector_probe.py scripts/selector_reasoning_probe.py scripts/compare_search_diagnostics.py scripts/cloud_smoke_recovery.py
git diff --check
```

必须使用工作树自己的 `.venv`。系统 Python 在这台 Windows 主机上缺少 `fastapi` 等项目依赖，
会在 pytest 收集阶段失败；这属于解释器选择错误，不是代码回归。不要把整个 `scripts`
目录直接作为 mypy 目标，否则脚本可能同时被识别成顶层模块和 `scripts.*` 模块。

如果完整测试依赖本机 PostgreSQL，使用独立临时测试库；不得连接云端或复用生产库。
记录实际命令、退出码和摘要，不要只写“测试通过”。

### 阶段 E：部署与 Smoke 门槛

只有在以下条件全部满足后才构建新镜像：

- 新失败用例先红后绿；
- 现有 selector、reasoning probe 和诊断脚本测试通过；
- 全量 pytest、Ruff、mypy、diff check 通过；
- 候选 prompt probe 达到预先写下的门槛，不能跑完再改门槛；
- 已准备可执行的 API-only 回滚命令；
- 已记录部署前 API 与 DB 容器 ID。

新 Smoke 的最低判断标准：

- 总分必须高于 `30.77`，或在用户明确接受的情况下用可解释的分项收益换取总分持平；
- direct recall 不应再次从 100 降为 0；
- abstention 不应退回 0；
- API 健康、重启计数为 0；
- DB 容器 ID 必须不变。

一次结果若只是在 direct recall 与 abstention 之间来回交换，应停止官方试跑，返回本地设计，
不要继续用付费 Smoke 搜索阈值。

## 10. 新候选的 API-only 部署模板

以下模板在服务器执行。`/opt/probe-29a3a13` 是当前实际检出目录。

```bash
set -euo pipefail
P=/opt/probe-29a3a13
ENV=/opt/masm-v11/config/v11.env
BRANCH=codex/masm-v11-selector-probe
git -C "$P" pull --ff-only origin "$BRANCH"
git -C "$P" status --short
SHA=$(git -C "$P" rev-parse --short=7 HEAD)
IMAGE="masm-v11-final-candidate:$SHA"
OVERRIDE="/opt/masm-v11/config/image.override.$SHA.yml"
docker build -t "$IMAGE" "$P"
docker image inspect "$IMAGE" --format 'IMAGE_OK id={{.Id}} size={{.Size}}'
printf '%s\n' 'services:' '  api:' "    image: $IMAGE" > "$OVERRIDE"
docker compose --project-name masm-v11 --env-file "$ENV" -f "$P/deployments/docker-compose.v11.yml" -f "$OVERRIDE" config --images
OLD_API=$(docker inspect --format='{{.Id}}' masm-v11-api-1)
OLD_DB=$(docker inspect --format='{{.Id}}' masm-v11-postgres-1)
docker compose --project-name masm-v11 --env-file "$ENV" -f "$P/deployments/docker-compose.v11.yml" -f "$OVERRIDE" up -d --no-deps --force-recreate api
```

等待健康并验证只替换 API：

```bash
for i in $(seq 1 30); do H=$(docker inspect --format='{{.State.Health.Status}}' masm-v11-api-1); echo "health=$H"; [ "$H" = healthy ] && break; sleep 2; done
NEW_API=$(docker inspect --format='{{.Id}}' masm-v11-api-1)
NEW_DB=$(docker inspect --format='{{.Id}}' masm-v11-postgres-1)
[ "$OLD_API" != "$NEW_API" ] && echo API_REPLACED
[ "$OLD_DB" = "$NEW_DB" ] && echo DB_UNCHANGED
docker inspect --format='image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' masm-v11-api-1
docker exec masm-v11-api-1 python -c 'from masm.retrieval.evidence_selector import PROMPT_VERSION; print(PROMPT_VERSION)'
```

若没有同时看到 `API_REPLACED`、`DB_UNCHANGED`、`health=healthy` 和
`restarts=0`，不要启动官方 Smoke。

## 11. 回滚模板

服务器仍保留前一个已知镜像 `masm-v11-final-candidate:76c7595` 及 override：

```text
/opt/masm-v11/config/image.override.76c7595.yml
```

API-only 回滚命令：

```bash
P=/opt/probe-29a3a13
ENV=/opt/masm-v11/config/v11.env
OLD_DB=$(docker inspect --format='{{.Id}}' masm-v11-postgres-1)
docker compose --project-name masm-v11 --env-file "$ENV" -f "$P/deployments/docker-compose.v11.yml" -f /opt/masm-v11/config/image.override.76c7595.yml up -d --no-deps --force-recreate api
NEW_DB=$(docker inspect --format='{{.Id}}' masm-v11-postgres-1)
[ "$OLD_DB" = "$NEW_DB" ] && echo DB_UNCHANGED
docker inspect --format='image={{.Config.Image}} status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}' masm-v11-api-1
```

回滚只用于服务异常或明确决定恢复上一候选，不代表 `76c7595` 已解决 multi-session
reasoning，也不应把它描述为最终版。

## 12. 新 Smoke 的安全遥测流程

在启动 Smoke 前记录 UTC 标记：

```bash
SHA=$(git -C /opt/probe-29a3a13 rev-parse --short=7 HEAD)
date -u +%Y-%m-%dT%H:%M:%SZ | tee "/tmp/smoke-$SHA-started-at.txt"
```

Smoke 完成后只生成脱敏诊断文件：

```bash
SHA=$(git -C /opt/probe-29a3a13 rev-parse --short=7 HEAD)
START=$(tr -d '\r\n' < "/tmp/smoke-$SHA-started-at.txt")
docker logs --since "$START" masm-v11-api-1 > "/tmp/smoke-$SHA-all.log" 2>&1
grep -F '"event": "search.completed"' "/tmp/smoke-$SHA-all.log" > "/tmp/smoke-$SHA-search-diagnostics.jsonl"
wc -l "/tmp/smoke-$SHA-search-diagnostics.jsonl"
```

随后只使用 `scripts/compare_search_diagnostics.py` 的输出进行对比。不要用 `cat`、`less`、
编辑器或模型读取全量日志。若评测产生了需要清理的数据，必须沿用
`scripts/cloud_smoke_recovery.py` 的“只读审计清单 -> 用户确认 -> 精确清理 -> 零残留验证”
流程；不能按时间范围直接批量删除。

## 13. 关键文件索引

| 文件 | 用途 |
| --- | --- |
| `src/masm/retrieval/evidence_selector.py` | selector 协议、模型调用、fallback 与 abstention |
| `src/masm/retrieval/evidence_pool.py` | 候选池构建与排序 |
| `src/masm/services/search_service.py` | Search 主路径及 selector 接入 |
| `src/masm/retrieval/diagnostics.py` | Search 脱敏遥测 |
| `scripts/selector_probe.py` | 基础合成 selector 探针 |
| `scripts/selector_reasoning_probe.py` | strict / chain prompt 合成对照 |
| `scripts/compare_search_diagnostics.py` | 只比较安全字段的遥测工具 |
| `scripts/cloud_smoke_recovery.py` | Smoke 数据只读审计和精确清理 |
| `tests/unit/retrieval/test_evidence_selector.py` | selector 单元回归 |
| `tests/unit/test_selector_probe_script.py` | 基础探针测试 |
| `tests/unit/test_selector_reasoning_probe_script.py` | reasoning probe 测试 |
| `tests/unit/test_compare_search_diagnostics_script.py` | 安全比较脚本测试 |
| `deployments/docker-compose.v11.yml` | v1.1 Compose 定义 |
| `docs/competition/masm-v11-cloud-smoke-recovery-report-2026-10-08.md` | 云端恢复、清理和 API-only 部署先例 |
| `docs/competition/masm-v11-handoff.md` | 更早阶段的总交接，不代表当前实时状态 |

## 14. 给 DeepSeek Harness 的首轮任务文本

可把下面这段作为新会话的第一条任务：

> 先阅读 `docs/competition/masm-v11-deepseek-harness-handoff-2026-10-10.md`，只读核对
> 当前分支、`84926f6` 部署状态、`acc5b8a` 代码行为和现有测试。不要读取隐藏评测内容、
> 原始日志或 `.env`，不要修改云端数据库，也不要先改 selector prompt。把 v7 selector 冻结为
> 基线，追踪 multi-session 数据从写入、检索、关联到回答生成的完整路径；先用完全公开的
> “跨会话可连接 / 不可连接”成对场景建立失败测试，再做最小实现。任何实现都必须先红后绿、
> 跑完全量 pytest/Ruff/mypy/diff check；未满足本地门槛前不要构建镜像或启动官方评测。

接手者应把本文当成“需要重新核对的状态快照”，而不是无需验证的指令集合。若 Git、容器、
镜像或评分页面与本文不一致，以新的只读证据为准，并在修改前记录差异。
