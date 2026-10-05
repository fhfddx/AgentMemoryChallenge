# MASM 最终比赛提交实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本文是跨阶段提交路线图；若出现新的时间语义架构或其他较大代码改动，须先另写专项设计与实施计划，不把路线图当作其详细设计。

**Goal:** 在不牺牲已有线上 v1.0 保底候选的前提下，选出有证据支持的最终版本，完成多模态开源方法榜的固定版本、稳定自托管 API、官方 Smoke 和一次审慎的 Full 提交。

**Architecture:** 两个候选并行保留：线上 v1.0 为保底，隔离的 v1.1 仅在质量、时延、容量、成本及合规门槛全部通过后升级为最终候选。先本机离线与受限正式模型复核，再冻结 Git/镜像/配置；只有获得用户针对外部操作的明确授权后，才做独立部署和官方提交。默认关闭的融合写入实验不属于当前发布路径。

**Tech Stack:** Python 3.11+、FastAPI、PostgreSQL/pgvector、Docker Compose/Caddy、pytest/Ruff/mypy、本机计量脚本。

**Spec:** `docs/competition/masm-v11-release-checklist.md`、`docs/competition/submission-checklist.md`、`docs/competition/masm-v11-metered-holdout.md`；当期官方 [赛事页](https://agentmemoryleaderboard.ai/competition/) 与 [API/参赛指南](https://agentmemoryleaderboard.ai/api-guide)。官网信息按 2026-10-05 核对，冻结和提交前须再核对。

## Global Constraints

- 只使用 `E:/Competitions/AgentMemoryChallenge/.worktrees/masm-v11` 的 `codex/masm-v11` 工作树；保留全部现有未提交改动，不新建工作树、不覆盖 v1.0 主仓库。
- 本计划编写阶段不调用 Provider、不运行容量/官方评测、不更改云服务器或生产库、不推送代码、不打印密钥。
- 后续本机测试只连接 `localhost/127.0.0.1/::1` 且库名严格为 `masm_test` 的隔离数据库；运行数据使用唯一前缀和精确清理清单。
- `MASM_FUSED_EMPTY_HISTORY_TEXT` 继续默认关闭；第 29 节自然语言多时间反例未解决前，不以该实验路径的旧三例时延作为发布收益。
- OpenAI 旧窗口专属实际账单保持 `unavailable/not_reconciled`，不再追查，也不能写成零。新测试只记录可得的 usage、调用和分币种估算，并遵守事先规定的请求/支出上限。
- 不以 Recall 命中替代事件时间忠实性、原子证据质量、跨用户隔离、真实图片质量或容量证据。
- 官网材料截至北京时间 2026-10-31 23:59；评测截至 2026-11-04 23:59。官网称第二次同赛道 Full 在首次完成 30 天后才开放，故本期按一次正式 Full 机会规划，预留 0.5–2 天运行缓冲。
- 涉及云端、生产备份、DNS、公开仓库推送、官网 Version/Smoke/Full 的步骤是未来执行门槛，**不是本次规划请求的操作授权**；执行前由用户审阅证据并明确同意。

## Review Focus

- 原文有文字日期、相对日期与数字日期并存时，事件时间不得因融合而错误写入或变空；任务 1 的逐条时间断言负责覆盖。
- 多事实、冲突更新、无证据拒答和图片检索在 v1.1 中不能靠增加返回条数伪装成质量提升；任务 2 的分项指标负责覆盖。
- 同 `request_id` 重试、并发写入、立即 Search、服务重启和删除必须保持幂等、持久与隔离；任务 3、5 的测试负责覆盖。
- 正式图片输入、图片查询、超大输入、无效 Key、上游 429/503 与响应大小边界需要明确结果；任务 2、3、5 负责覆盖。
- Git 提交、镜像摘要、运行档位和官网 Version 必须指向同一候选，不能混入 `.superpowers`、密钥或测试原文；任务 4、5 负责覆盖。

---

### Task 1：锁定安全候选与时间语义（目标 10 月 8 日）

**Files:** 核对 `src/masm/orchestration/add_pipeline.py`、`src/masm/agents/fused_text.py`、`src/masm/config.py`、`tests/integration/test_fused_text_pipeline.py`；记录到 `docs/competition/masm-v11-metered-holdout-report.md` 与发布清单。若需要新的融合架构，另建专项 spec/plan。

**Interfaces:** 正式档位仍走现有 Add/Search 契约；融合开关为 `Settings.fused_empty_history_text: bool = False`。不能把 `has_one_supported_date()` 当作时间语义唯一性的证明。

- [ ] 只读核对 Git 分支/HEAD、改动清单、`MASM_FUSED_EMPTY_HISTORY_TEXT` 默认值与打包配置；记录真实状态，不清理未提交文件。
- [ ] 预先固定时间反例表：数字/文字双日期、相对词与绝对日期并存、标识符中的数字、跨会话改期、有历史与无历史、无日期。逐条定义预期事件时间和旧路径回退，而非只断言 Recall。
- [ ] 用 Fake Provider 写/补回归断言；失败先行验证确有缺口，随后只修复正式路径中的确定性缺陷。若修复需要开放实验融合，应停止并单独设计，不继续加时间词正则。
- [ ] 在本机隔离 `masm_test` 跑定向数据库测试、完整 pytest、`ruff check .`、`mypy src`，记录退出码、警告和 Windows 临时目录告警。能用独立 `--basetemp` 避开退出告警，但不声称旧 reparse point 已修好。
- [ ] **Gate 1:** 正式路径无已知时间/隔离回归、融合默认关闭；否则 v1.1 不进入后续正式模型对照，v1.0 保底不受影响。

### Task 2：预注册的质量、用量与时延对照（目标 10 月 14 日）

**Files:** 文本用 `scripts/metered_holdout.py`，图文/图片质量用 `scripts/evaluate_synthetic_recall.py`；后者目前不计量图片 Provider usage，须先设计并测试限定的媒体计量扩展，不能借用纯文本费用。依据 `docs/competition/masm-v11-metered-holdout.md` 执行；结果写入新的中文版本决策记录，不覆盖旧 JSON/TSV。计量改动先在 `tests/unit/experiments/test_metered_holdout.py` 与 `tests/integration/test_metered_holdout_run.py` 做失败先行测试。

**Interfaces:** 相同冻结样本、哈希、run-tag、模型/配置与顺序规则比较 v1.0/v1.1；报告聚合指标，不保存请求正文、图片或 Key。

- [ ] 在运行前冻结一份未按结果挑选的独立来源样本清单：文本按多事实、改期/冲突、干扰、跨会话、拒答等类别分层；媒体另选图文、纯图与图片查询。写明每类样本数、来源许可、抽样规则、预期证据与 SHA-256，并在看到结果前写定质量差值、单次时延、Provider 请求数和总支出上限。与既有 3 案例 Memora 和项目自编六类分开报告；现有 `external` 模式只支持纯文本，不能用它声称覆盖媒体。
- [ ] 先以 Fake/数据库集成测试核对计量字段、失败分类、精确删除和四表/资产零残留；清理失败即停止下一版。
- [ ] 在事前确定的少量请求上做正式 Provider 同源对照；分项记录标准 Recall@10/100、原子证据覆盖、错误时间字段、误召回、返回条数/字节、Add/Search 分布与 P95、LLM/Embedding/图片调用、usage 缺失和失败重试。图片费用只有媒体计量通过后才报告；P95 附样本数，不外推容量。
- [ ] 逐条人工复核有争议的时间与图片结果；只读汇总脱敏报告，核对运行前后本机 `masm_test` 四表及资产目录。旧 OpenAI 账单不作为重跑理由；缺失实际金额标 `unavailable`。
- [ ] **Gate 2:** 按运行前写定的阈值审查，任何跨用户泄漏、事件时间静默错误、图片/文本关键证据丢失或清理失败均一票否决。若 v1.1 仍只在自编模板的一项原子覆盖有收益，或独立样本的质量增益不足以抵偿事先限定的写入时延/请求成本，则停止 v1.1 发布路线，直接转 Task 4 的 v1.0 保底准备。不能仅凭 583 项测试或历史三例融合提速放行。

### Task 3：有条件的性能与可靠性收尾（目标 10 月 23 日）

**Files:** 若 Gate 2 值得继续，优先审查 `src/masm/orchestration/add_pipeline.py`、Provider Client、`src/masm/retrieval/` 和计量报告；相关改动由对应 `tests/unit/`、`tests/integration/`、`tests/contract/` 测试覆盖。容量工具为 `scripts/capacity_test.py`，但只有获准后才运行。

**Interfaces:** 公共 Add/Search JSON、用户隔离和默认关闭的融合开关不变。任何性能变更要有同源质量断言和可归因的阶段耗时对照。

- [ ] 先按阶段计时定位真实长尾：感知、召回、时序、治理、Embedding、提交、Search；分清模型等待、失败重试与数据库耗时。仅选择不丢时间证据/记忆治理的改动。
- [ ] 对每个确定的性能缺陷执行一轮失败先行测试、最小实现、定向与全量复测；若需重构时间处理，单独走设计/实施计划，不在截止前追加启发式补丁。
- [ ] 经用户审阅质量/费用并授权后，先在隔离环境递增负载，再按发布清单做 Add 16、Search 16 容量验证；记录错误率、P95、资源、上游 429/503、重试、30 MiB 响应上限与 Top K 100。不得触及 v1.0 生产数据库。
- [ ] 用公开指南的同步 Add、立即可检索、同 `request_id` 重试和 `user_id` 唯一隔离要求验收。容量不足就降低并如实申报可支持的并发；不能假报 16/16。
- [ ] **Gate 3（最迟 10 月 24 日）:** 综合独立质量、图片结果、可观察用量、容量和运行成本选唯一候选。v1.1 任一硬门槛失败就选 v1.0；不得为赶日期跳过门槛。

### Task 4：固定版本与提交材料（目标 10 月 27 日）

**Files:** `README.md`、`THIRD_PARTY_NOTICES.md`、`deployments/README.md`、`docs/competition/submission-checklist.md`、`docs/competition/masm-v11-release-checklist.md`、`deployments/docker-compose.v11.yml`、`deployments/Caddyfile`、`.env.example`；证据独立保存在不含私密内容的中文发布记录。

**Interfaces:** 公开仓库固定 Commit ↔ 构建镜像 digest ↔ 运行配置摘要 ↔ 官网 Version 一一对应；Prompt 作为 wheel 包资源。若最终选 v1.0，只准备其现有线上版本的相同对应关系，不硬上 v1.1。

- [ ] 在当前脏工作树逐文件审阅差异与未跟踪文件；只把候选代码、测试、必要文档纳入候选，排除 `.env`、`.superpowers`、JSON/TSV、样本正文、密钥及数据库/图片产物。不得使用会丢失用户改动的 reset/checkout。
- [ ] 复核当期官网多模态开源方法榜的模型和图像处理约束；对“`gpt-4o-mini` 结构化图片后由 `text-embedding-v4` 生成文本向量”的合规疑点取得组委会明确答复或保持未确认，不自行宣称已批准。
- [ ] 用占位环境做两版 Compose/Caddy 静态校验，检查 Alembic `0005`、轮转、端口/卷/网络隔离、Prompt 分发、公开仓库归属与原方法/改动披露；再跑最终全量 pytest、Ruff、mypy、密钥/隐私扫描和 `git diff --check`。
- [ ] 用户审阅后冻结唯一 Git Commit、不可变版本标识、镜像 digest、模型/Prompt/配置摘要及回滚方案；提交和推送仅在此关口按授权执行。冻结后不再把新功能混入同一 Version。
- [ ] **Gate 4:** 材料齐全且可复现。官方要求开源方法同时提供公开仓库固定 Commit 和自托管稳定 Add/Search API；仅有 Docker 包或本机测试不算完成提交。

### Task 5：隔离部署、官方 Smoke、Full 与复核（目标 10 月 28 日—11 月 4 日）

**Files:** 按 `docs/competition/masm-v11-release-checklist.md` 执行部署/回滚；使用固定版本与官网 Version 记录。此任务全是未来的显式授权关口，当前不执行。

**Interfaces:** v1.0 域名/API/Postgres/资产保持原状。若选 v1.1，使用独立数据库、资产卷、Key 与 `v11` 域名；官网 Memory System Key 通过受控表单绑定，不放入 URL、聊天、日志或仓库。

- [ ] **先取得用户明确批准云端操作。** 对 v1.0 数据库和资产卷作一致性备份、SHA 校验与隔离恢复演练；核对线上 v1.0 Commit/镜像摘要。失败即停止部署。
- [ ] 若选 v1.1，仅并行启动独立 Compose，核对 Health、HTTPS、401、正式文本/图文/纯图 Add/Search、重试与重启持久性；逐项验证旧站点仍健康。异常只撤 v1.1 站点/API，保留其卷诊断，不用 `down -v`。
- [ ] **先取得用户对官网操作的明确批准。** 完整填写多模态开源方法榜的系统、版本、固定 Commit、公开仓库、Add/Search URL、鉴权、容量和方法归属材料；核对评测 Key 状态与该赛道已使用的 Full 次数。
- [ ] 运行一次对应固定 Version 的官方 Smoke；失败只依据平台错误修复接口或配置，必要时新建可追溯版本，不偷偷改变已绑定镜像。Smoke 通过且用户审阅最终证据后，才启动一次正式 Full；建议最迟 10 月 30 日开始，为官网估计的 0.5–2 天及 11 月 4 日截止留缓冲。
- [ ] 监测运行状态、服务健康、用量与成本上限；只记录脱敏 Run/Job ID、阶段、错误类别。Full 失败优先按官网断点续跑/申诉路径，不以换 Key 或重复 Version 绕过 30 天限制。
- [ ] 完成后核对私有结果、版本/镜像/Commit 与审核状态；在评测结束后按官网数据要求于 30 天内删除评测数据及派生副本，保留不含内容的审计摘要。不能把“Full 已发起”称作“有效提交”；有效状态需 Full 完成和平台复核。

## 每周决策点与当前状态

| 截止目标（北京时间） | 应有产物 | 决策 |
| --- | --- | --- |
| 10 月 8 日 | 时间语义回归表、全量离线验证 | 确认安全候选，融合仍关闭 |
| 10 月 14 日 | 预注册质量/图片/用量对照与清理证明 | 决定 v1.1 是否继续投入 |
| 10 月 24 日 | 性能/容量证据或明确缺口 | 在 v1.1 与 v1.0 间唯一选版 |
| 10 月 27 日 | 固定 Commit、镜像/配置映射及完整材料 | 审阅后才允许推送/部署 |
| 10 月 30 日 | 已批准的独立部署与 Smoke | 仅全部通过才启动 Full |
| 10 月 31 日 23:59 | 官网申请及完整材料 | 官方硬截止，不能依赖临界提交 |
| 11 月 4 日 23:59 | Full 完成、故障续跑/复核材料 | 官方评测关闭 |

**当前已验证（由 2026-10-05 执行账本更新）：** v1.0 线上候选有历史公网验证；v1.1 本机已有文本、图文和纯图极小样本。2026-10-05 在隔离本机 `masm_test` 上新鲜完整回归为 `602 passed`、74 条既有弃用警告、退出码 0；Ruff/mypy 的最后通过记录见发布清单第 21 节，**本次未重跑静态检查**。融合开关默认关闭。新的版本口径、同档位结果与清理范围见 `docs/competition/masm-final-candidate-evidence-ledger-2026-10-05.md`；该路线图已由 v2 执行计划取代。

**当前未验证：** 独立样本上的普遍质量收益和事件时间忠实性、当前代码的正式模型时延、图片可审计用量、容量、组委会对图像向量路径的确认、线上 v1.0 构建映射、v1.1 独立环境、官网 Key/Version/Smoke/Full。不能因时间表到达而自动将这些项目勾选。
