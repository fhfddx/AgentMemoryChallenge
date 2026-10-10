# MASM v1.1 DeepSeek Harness 交接（2026-10-10）

## 0. 更新：DeepSeek Harness 第二轮（v5 → v7）

> **本节取代第 3、4、5、6、7 节的状态描述。** 那些小节记录的是 `0fc322d` 快照；
> 代码、协议与 prompt 版本都已前进，线上服务未变。第 2 节的边界与第 8 节的安全比较
> 要求仍然有效。

### 0.1 当前代码状态

| 项目 | 当前值 |
| --- | --- |
| 工作树 | `E:\Competitions\AgentMemoryChallenge\.worktrees\masm-v11` |
| 分支 | `codex/masm-v11-selector-probe` |
| 交接实现基线 | `1fb43fc`（本次元数据修订前的交接快照） |
| 最新代码提交 | `acc5b8a`（最后一个改动 `src/` 或 `scripts/` 的提交） |
| 远端基线 | `origin/codex/masm-v11-selector-probe` = `b8c1920`（**尚未推送**） |
| 未推送提交 | 共 6 个；用 `git rev-list --count origin/codex/masm-v11-selector-probe..HEAD` 实时复核 |
| selector prompt version | `evidence-selector-v7` |
| selector 决策协议 | 三态 `evidence_state ∈ {sufficient, partial, insufficient}` |
| 线上镜像 / 提交 | `masm-v11-final-candidate:0fc322d`（**未改动**） |
| 新版本的官方 Smoke | **尚未运行** |

`b8c1920` 之后共有 6 个未推送提交：`1fb43fc`（文档）、`acc5b8a`、`eb33e6e`、
`0e783f1`、`0292f3b`，以及承载本段元数据修正的文档提交。其中 4 个改动代码，
2 个只改文档。承载本段修正的提交 SHA 不写入本文，以免提交内容与其自身 SHA 形成自引用；
需要时用 `git rev-parse --short HEAD` 查询当前分支顶端。

| 提交 | 作用 |
| --- | --- |
| `0292f3b` | 二态 `sufficient_evidence` 换成三态 `evidence_state`：`partial` 返回已验证索引且不回落强锚点，`insufficient` 一律拒答；旧二态 Provider 输出经 `model_validator(mode="before")` 映射（`false → insufficient`），旧字段既不进 schema 也不发给模型；遥测新增固定枚举 `selector_evidence_state` |
| `0e783f1` | v6 收窄 `partial`：必须能指名一条被选记忆逐字陈述的被询问事实，不得因「不确定」或「看起来相关」而报 partial；关系型问题在其记忆覆盖全部事实与链路时允许 `sufficient` |
| `eb33e6e` | v7 把 `evidence_state` 放到 schema 属性首位。严格解码按属性顺序生成，原先 `selected_indices` 在前，模型在判定「不足」之前就已写好索引；调序后 `insufficient` + 非空 indices 的矛盾消失 |
| `acc5b8a` | 首轮 `partial` 也用更深候选池（32→48）重试；深层轮只在给出模型确认的完整 selection 时才替换首轮，否则原样保留首轮 partial。`selector_reasoning_probe.py` 增加 `production_eligible` 与 `--strict-only` 生产门槛，`candidate_eligible` 降为记录项 |
| `1fb43fc` | 只更新本文档，不改源码或测试 |
| 本段元数据修订提交（SHA 不在本文内固定） | 修正交接元数据与审计证据，只改本文档 |

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

v7 三条门槛均按字面达成。`candidate_eligible` 仍为 `False`，因为生产 prompt 已覆盖
chain 变体的用例；它表示「chain 优于 strict」，**不是**部署门槛。`chain` prompt 只作对照
基线，永远不是部署候选。

### 0.3 仍未闭环的风险

**Provider `unavailable` 会退回全量 question-admitted 强锚点。** 约 220 次合成调用中观察到
3 次 `selector_failure_category=unavailable`，每次结果都是 `selector_evidence_state=unknown`、
`selector_fallback=true`、返回最多 12 条强锚点。这是 `0fc322d` 已有的 v4 行为，与三态改动
无关，但会让一个本该拒答的问题带着未经模型确认的证据被作答，直接影响 abstention 分项。
本轮刻意没有一并修改，以保持「一次只改一个变量」；它应作为下一个独立候选并自带预登记门槛。

### 0.4 部署前仍缺的步骤

1. 尚未对新版本运行任何官方 Smoke；最新公开总分仍是 `30.77` 的旧记录。
2. 尚未在服务器构建镜像、替换 `masm-v11-api-1`，也未生成新的脱敏遥测。
3. 第 8 节那两个 14 行诊断文件的安全比较**仍未闭环**，不要写成已解决。
4. 仓库与 `known_hosts` 都没有可用的 v11 服务器 SSH 目标，部署命令需要在服务器控制台执行。

## 1. 文档用途与当前结论

本文是给后续 DeepSeek Harness 会话的独立交接快照。它聚焦 2026-10-10
围绕 evidence selector 的实验、云端部署和官方 Smoke 结果，不替代仓库中更早的
总交接、发布清单和云端恢复报告。

当前线上候选为 `0fc322d`，服务健康且数据库容器未被替换，但它不是已确认的最终最优版：

- 官方 Smoke 总分仍为 `30.77`；
- abstention 从此前的 `11.11` 提升到 `22.22`；
- direct recall 同时从 `100.00` 降到 `0.00`；
- multi-session reasoning 仍为 `0.00`。

因此，下一步的核心不是继续扩大 selector 候选池，也不是直接部署 chain-aware prompt，
而是先区分“有可用但不完整的直接证据”和“确实应该拒答的证据不足”。当前二元协议
`sufficient_evidence + selected_indices` 无法稳定表达这个边界。

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
| 当前代码提交 | `0fc322d` |
| 远端状态（写本文前） | 本地与 `origin/codex/masm-v11-selector-probe` 同步 |
| 服务器检出目录 | `/opt/probe-29a3a13` |

服务器目录名保留了旧提交号，但目录内 Git HEAD 已前进到当前提交。不要根据目录名判断
实际版本，必须使用 `git -C /opt/probe-29a3a13 rev-parse --short=7 HEAD`。

当前相关提交从新到旧如下：

| 提交 | 作用 |
| --- | --- |
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
| 当前镜像 | `masm-v11-final-candidate:0fc322d` |
| 当前 override | `/opt/masm-v11/config/image.override.0fc322d.yml` |
| 环境文件 | `/opt/masm-v11/config/v11.env` |
| Compose 文件 | `/opt/probe-29a3a13/deployments/docker-compose.v11.yml` |
| selector prompt version | `evidence-selector-v4` |

最近一次部署后的已验证状态：

- API 容器 ID 已变化，说明 API 被替换；
- PostgreSQL 容器 ID 未变化；
- API 为 `running`、`healthy`、`restarts=0`；
- 容器内 `PROMPT_VERSION` 为 `evidence-selector-v4`。

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

最新 `0fc322d` Smoke 的页面状态为成功，耗时 `6m32s`，完成时间为
`2026-10-10 13:19:38`。

可以从聚合分数得出的结论：

- 矛盾输出统一拒答确实提高了 abstention；
- 同一改动也损失了 direct recall；
- 总分没有提高；
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
2. `sufficient_evidence=false`：无论 indices 是否为空都返回空证据并
   `abstained=true`；如果 indices 非空，同时标记 `invalid_output`。
3. `sufficient_evidence=true`：再检查 indices 是否为空、越界、重复或超过上限；
   非法时走 strong-anchor fallback。
4. 合法时返回所选候选。

`0fc322d` 改的是第 2 步。此前矛盾输出会走 strong-anchor fallback，可能在模型明确给出
`sufficient_evidence=false` 时仍返回证据；现在统一为空。这一规范化更安全，但官方 Smoke
表明它对 direct recall 过于激进。

对应失败先行回归测试位于：

```text
tests/unit/retrieval/test_evidence_selector.py
test_inconsistent_insufficient_decision_normalizes_to_safe_abstention
```

写本文前，`0fc322d` 已完成以下验证：

- 完整测试：`753 passed, 1 skipped, 1 warning in 47.31s`；
- Ruff 通过；
- mypy 通过，共检查 56 个源文件；
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

## 8. 尚未解决的安全遥测比较问题

最新 Smoke 的文件约定如下：

```text
/tmp/smoke-0fc322d-started-at.txt
/tmp/smoke-0fc322d-all.log
/tmp/smoke-0fc322d-search-diagnostics.jsonl
/tmp/smoke-v2-search-diagnostics.jsonl
/tmp/smoke-v2-0fc322d-compare.txt
```

已看到 `wc -l` 对两个 JSONL 文件都返回 14 行，但随后比较脚本却输出：

```text
v2 MISSING /tmp/smoke-0fc322d-search-diagnostics.jsonl
```

`scripts/compare_search_diagnostics.py` 只在 `Path.exists()` 为 false 时输出 `MISSING`，
因此当前最可能是 VNC 手工输入时的路径、续行或不可见字符问题，而不是脚本解析失败。
这个矛盾尚未闭环，不要在交接后把它写成已比较成功。

先在服务器运行以下两个单行命令：

```bash
ls -lb /tmp/smoke-v2-search-diagnostics.jsonl /tmp/smoke-0fc322d-search-diagnostics.jsonl
python3 /opt/probe-29a3a13/scripts/compare_search_diagnostics.py /tmp/smoke-v2-search-diagnostics.jsonl /tmp/smoke-0fc322d-search-diagnostics.jsonl | tee /tmp/smoke-v2-0fc322d-compare.txt
```

只分析比较脚本输出的安全字段。不要把 `/tmp/smoke-0fc322d-all.log` 的原文或 JSONL
中的额外字段复制到 Harness 对话。

现有可用 baseline 文件的安全汇总曾显示：14 行中 `candidate_zero=3`、
`returned_zero=9`、`abstained=8`、`fallback=0`、失败类别均为 `none`。这只是可用基线文件的
聚合状态；在成功完成上述比较前，不要声称它与最新 `0fc322d` 的逐行差异已经确定。

## 9. 推荐给 DeepSeek Harness 的下一步

### 阶段 A：只读复核，不改代码

1. 核对 Git 分支、HEAD 和工作树是否干净。
2. 阅读本文及下列关键文件，不要先改 prompt：
   - `src/masm/retrieval/evidence_selector.py`
   - `src/masm/retrieval/evidence_pool.py`
   - `src/masm/services/search_service.py`
   - `src/masm/retrieval/diagnostics.py`
   - `tests/unit/retrieval/test_evidence_selector.py`
   - `scripts/selector_reasoning_probe.py`
   - `scripts/compare_search_diagnostics.py`
3. 在服务器用第 8 节的单行命令闭环安全遥测比较。
4. 明确列出哪些判断是代码事实、合成测试事实、官方聚合事实或推断。

### 阶段 B：先建立新的合成失败用例

在实现前，至少增加两个彼此对照的合成场景：

1. **部分直接证据**：模型认为不足，但返回的 index 含有明确、直接、可引用的事实；目标是
   不因布尔值矛盾而无条件丢弃全部召回。
2. **缺失链路证据**：模型认为不足且 index 只是局部链路；目标是维持拒答，不能恢复成
   旧式无条件 fallback。

测试必须先在当前 `0fc322d` 上至少有一个失败，再实现。不要使用或仿造隐藏题。

### 阶段 C：评估三态协议，而不是继续堆提示词

优先评估把 selector 决策从二态改为三态：

```text
sufficient   -> 可以返回完整证据
partial      -> 有直接支持，但不足以完成全部推理
insufficient -> 应拒答
```

这只是待验证的架构方向，不是已批准方案。设计时需要回答：

- `partial` 是否允许返回证据，返回多少；
- Search API 当前是否能表达“部分证据”而不越权替用户作答；
- 哪些确定性信号能防止 missing-link 被误判成 partial；
- 如何兼容 Provider 的旧二字段输出；
- 遥测如何只记录枚举和计数，不记录内容；
- prompt version、Pydantic schema、回退逻辑和测试怎样同步迁移。

若三态会扩大 API 语义或数据泄漏风险，则退回到更窄的确定性仲裁器方案，但同样必须用
“部分直接证据 / 缺失链路”成对测试证明，而不是凭阈值猜测。

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
> 当前分支、`0fc322d` 的 selector 行为、相关测试和安全遥测比较脚本。不要读取隐藏评测
> 内容、原始日志或 `.env`，不要修改云端数据库，也不要先改 prompt。先闭环第 8 节中
> 两个 14 行诊断文件的安全比较，再用公开合成数据为“部分直接证据”和“缺失链路证据”
> 各写一个成对失败测试，评估二态协议是否需要改成三态。任何实现都必须先红后绿、跑完
> 全量 pytest/Ruff/mypy/diff check；未满足本地门槛前不要构建镜像或启动官方评测。

接手者应把本文当成“需要重新核对的状态快照”，而不是无需验证的指令集合。若 Git、容器、
镜像或评分页面与本文不一致，以新的只读证据为准，并在修改前记录差异。
