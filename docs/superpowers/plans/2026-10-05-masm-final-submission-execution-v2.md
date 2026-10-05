# MASM 最终比赛提交执行计划 v2

> **For agentic workers:** 执行本计划时使用 `superpowers:executing-plans`，逐项记录证据与停止原因。本文件是提交路线图，不授权云端、生产库、公开推送或官方评测；较大功能改动另立专项设计及实施计划。

**Goal:** 在北京时间 2026-10-31 23:59 材料截止前完成可复现的多模态开源方法榜提交材料，并在 2026-11-04 23:59 评测关闭前，谨慎完成固定版本的官方 Smoke 与一次 Full；若 v1.1 不能在预定门槛内证明价值，采用经重新核验的 v1.0 保底候选。

**Architecture:** 本机证据与版本选择 → 干净固定提交/可审计镜像 → 获授权的独立线上部署与容量声明 → 官网材料/Version → Smoke → Full → 结果及数据收尾。`submission-rc2` 是本机 v1.0 标签，不自动等于当前线上镜像；历史 B0 `official-baseline` 不等于 MASM v1.0。

**Tech Stack:** Python/FastAPI、PostgreSQL/pgvector、Docker Compose/Caddy、pytest/Ruff/mypy、本机受限计量脚本。

**Evidence:** `docs/competition/masm-v11-release-checklist.md` §19–24、`docs/competition/masm-v11-metered-holdout-report.md` §39–44、`docs/competition/masm-v11-handoff.md`、`docs/competition/submission-checklist.md`。旧版路线图：`docs/superpowers/plans/2026-10-05-masm-final-submission-roadmap.md`；以本 v2 的状态和门槛为准。官方 [赛事页](https://agentmemoryleaderboard.ai/competition/)、[参赛规则](https://agentmemoryleaderboard.ai/rules)、[API 指南](https://agentmemoryleaderboard.ai/api-guide/)于 2026-10-05 核对，正式冻结和提交前再核对。

## 0. 当前基线：已验证与待验证

| 项目 | 2026-10-05 已验证 | 不得据此推断／待验证 |
| --- | --- | --- |
| Git | 现用 `codex/masm-v11` 工作树，HEAD `1c7b26a8a0e924cb9343a5afdc47185b25b0841b`，有既存已跟踪及未跟踪改动；本机 `submission-rc2` 解引用为 `db7d75a95c2c6a119425ed1b378023297aac03ce` | 未提交改动不是可发布固定 Commit；线上 Commit、镜像 digest 和配置尚未重新核对 |
| 离线质量 | 当前 v1.1 代码最后一次完整回归 `602 passed`、74 条既有弃用警告；Ruff、mypy（52 个源码文件）与 diff 检查通过。融合空历史文本开关默认关闭 | 非官方质量、跨用户压力、真实图片语义与线上持久性证明；计划发布时必须重跑 |
| 同档位文本 | `official-masm` 六个自编案例配对：v1.0/v1.1 Add 均值 4875.58/3154.34 ms，Add P95 5679.08/4067.80 ms；Search P95 1241.01/1246.55 ms；LLM 21/16，Embedding 18/13 | 样本小且自编；不代表总体 P95、容量或独立质量收益 |
| 外部来源诊断 | Oracle 摘录构成的跨案例干扰 3 例：两版正题字面 Recall@10、原子覆盖均为 1；严格无证据题均未做到空返回，均回了 2 条无关记忆；清理核对通过 | 跨案例混排与无证据题仍是项目构造，非官方完整长历史；未证明 v1.1 独立正向收益或最终回答泄漏 |
| 图像与费用 | 合成媒体路径走通且有媒体调用计数；新运行保留分币种 usage/估算。用户已决定不再追查历史 OpenAI MASM 专属账单 | 真实图像语义和模型合规未确认；历史 OpenAI 实付为 `unavailable/not_reconciled`，绝不能当零；小样本费用不外推总成本 |
| 环境 | 本机隔离 `masm_test` 的已记录运行完成精确行数/资产清理，测试容器停止；四个原有业务容器最后检查健康。v1.0 标签 148 个跟踪文件做过有限敏感文件扫描；Compose/Caddy 静态校验通过 | 不代表全量密钥审计；`.dockerignore` 未排除 `.worktrees/`、`.superpowers/`、`.env.local` 的构建上下文风险未关闭；云端、生产库、容量、官网 Version/Smoke/Full 均未验证 |

## 1. 不可跨越的边界与决策规则

- 只在现有 `E:/Competitions/AgentMemoryChallenge/.worktrees/masm-v11` 工作树推进；保留全部未提交改动，不新建工作树，不对用户文件做 reset/checkout。每步先记录分支、HEAD、`git status --short`，再写变更。
- 本机数据库集成/正式模型诊断只用 localhost/127.0.0.1/::1 的 `masm_test`，唯一 run-tag；记录四表及资产清理前后。不得连接生产库、打印 Key、复制原始评测内容进公开仓库。
- `MASM_FUSED_EMPTY_HISTORY_TEXT=false` 保持发布默认。自然语言多时间事件反例未解决前，实验融合路径不纳入候选；不以粗糙时间词正则抢期限。
- 不再为已达到量尺上限的相同小样本或不可区分的合成组合付费重跑。新的受限正式模型运行，必须在运行前固定样本、预期证据、质量/延迟判据、请求/费用停止线和精确清理步骤。
- v1.1 的 **go** 必须同时满足：无硬性时间/用户隔离/图片证据回归；有独立且可区分的正向证据，收益足以抵偿延迟与调用成本；模型与图片路径合规明确；能通过发布/容量门槛。任何条件缺失，到 10 月 10 日即按 **no-go** 转 v1.0 保底，不为赶期限主观豁免。
- 官方指南允许申报实际可支持的并发容量，**16 Add / 16 Search 是原计划的本地目标而非官网固定最低要求**。只申报经隔离负载验证的数字。未经用户单独批准不运行容量测试。
- 线上只读审计、组委会咨询、生产备份/部署、公开推送、官网申请/Version、Smoke、Full 都分别需要明确授权；对一个动作的许可不自动扩展到下一个。Full 按本赛期实际一次机会规划，不以新 Key/版本绕过平台限制。

## 2. 日程与关键路径（北京时间）

| 截止目标 | 阶段产物 | 未通过时的动作 |
| --- | --- | --- |
| 10 月 06 日 | 证据账本、候选差异与合规问题定稿 | 不新增付费样本；列清缺口 |
| 10 月 10 日 | v1.1 go/no-go；默认保底 v1.0 | no-go 即停止 v1.1 发布优化，转保底打包 |
| 10 月 15 日 | 单一候选的本机测试、干净构建上下文、固定文件清单 | 不冻结/不推送有歧义的内容 |
| 10 月 20 日 | 经授权的线上身份核对、备份恢复与隔离部署验证 | 不动既有生产服务；保留本机候选 |
| 10 月 24 日 | 固定 Commit↔镜像 digest↔配置↔回滚证据；容量如实申报 | 不填未核实的容量或镜像身份 |
| 10 月 27 日 | 官网完整材料和 Eval Key 状态；预留审批/补件 | 立即处理材料问题，不能把申请当提交 |
| 10 月 29–30 日 | 经授权的官方 Smoke；仅通过后申请 Full 授权并启动 | 失败仅修契约/配置并重新冻结映射 |
| 10 月 31 日 23:59 | 官网材料硬截止 | 截止前不能仅留草稿 |
| 11 月 04 日 23:59 | Full 完成或按规则续跑，证据归档 | 未完成必须如实报告，不能称有效提交 |

官网预计 Full 可耗约 0.5–2 天；第 2 次同赛道 Full 通常要等首次完成 30 天，时间缓冲应前置。上述日期是内部目标，官方日期以提交前再次核对为准。

## Task 1：收口 v1.1 证据，确定唯一候选（10 月 06–10 日）

**Files / interfaces:** `docs/competition/masm-v11-metered-holdout-report.md`、`docs/competition/masm-v11-release-checklist.md`、`scripts/metered_holdout.py`、`src/masm/orchestration/add_pipeline.py`、`src/masm/agents/fused_text.py`、相关 `tests/unit/` 与 `tests/integration/`。正式 Add/Search 契约不变，融合仍关闭。

- [x] 建一页「证据账本」：区分 B0、`official-masm` 本机源码与公网 v1.0 镜像；列每条指标的样本数、来源、日期、档位、失败/usage 缺失、清理状态。将旧路线图的 `583 passed` 更新为有日期的 `602 passed` 历史记录，不写成此刻刚跑。
- [x] 核对现有测试覆盖的文字日期、数字日期、相对/绝对日期并存、跨会话改期及无历史/有历史分支；发现确定性正式路径缺陷时，先以失败测试定位，再作最小修复并复测。涉及融合架构另开 spec/plan，不能将实验开关顺手开启。
- [x] 先做只读样本可得性与许可核对。只有找到 **新且有区分力** 的独立来源样本（真实图像语义/图片查询，或明确的事件时间真值），才写运行前协议：固定样本与哈希、随机/顺序规则、原子证据/时间真值、两版相同 `official-masm` 档位、样本数与最小可判别差值、费用/请求硬停止线。找不到即停止新增付费诊断。
- [x] 如协议可行，先用 Fake Provider 和隔离 DB 检查计量与清理，再做一轮限量正式 Provider 配对；分项记录 Recall、原子证据、错误时间、无证据误召回、图片语义、返回字节、Add/Search 延迟分布与 P95 样本数、LLM/Embedding/媒体尝试及费用估算。不得把未对账金额叫实际扣费。
- [x] 运行后逐项检查四表、资产目录和原有容器；清理失败或跨用户泄漏立即停止。中文判定表逐条写「已验证/未验证/否决原因」。
- [x] **Gate 1:** 最迟 10 月 10 日明确 `v1.1 go` 或 `v1.0 fallback`。现有证据本身不足以给 v1.1 go；没有新的独立区分证据即选 v1.0，不继续为 v1.1 时延优化消耗提交窗口。

2026-10-05 执行结果：独立 CC0 真实图片同档位配对为 v1.0 0/3、v1.1 3/3，
通过事前延迟、安全、usage 和清理门槛；Gate 1 判定 `v1.1 go`，停止追加付费样本。

## Task 2：模型合规、版本来源与保底可用性（与 Task 1 并行准备）

**Files / interfaces:** `docs/competition/submission-checklist.md`、`docs/competition/public-deployment-validation-2026-09-30.md`、`docs/competition/masm-v11-release-checklist.md`、`README.md`、`THIRD_PARTY_NOTICES.md`、`deployments/README.md`。仅记录官方答复和版本映射，不改变公开契约。

- [x] 再次核对官网当前多模态开源赛道、固定公开 Git Commit、自托管 API、Add/Search、图片输入输出、模型限制、密钥和容量字段。将原方法引用、MASM 改动与模型用途写清楚。
- [ ] 把「`gpt-4o-mini` 提取图片文字/语义，再用 `text-embedding-v4` 对描述建向量」整理成可由组委会回答的是/否问题；**仅在用户明确授权外部联络后**发送，不把未回复推断为允许。若答复否定或截止前无答复，按保守解释调整方法并重新验证，或明确标注合规未过、不做 Full。
- [ ] **仅在用户授权只读云审计后**核对线上 v1.0 的运行镜像 digest、镜像构建来源/Commit、当前 Compose/环境档位（只记非敏感摘要）、域名/Health 与 `submission-rc2` 的对应。历史 `fe540e0` 构建记录不能代替当前核对；不得读出或记录密钥值。
- [ ] **Gate 2:** 任何候选都必须有可复核的模型合规结论、明确源码↔镜像来源；若 v1.0 线上身份不明，先解决映射，不能因为它是“保底”就直接提交。

2026-10-05 规则裁定：官网已明确内部架构不限定、原图优先/caption 兼容，且学术榜
全部 LLM/Embedding 分别限定为 `gpt-4o-mini`/`text-embedding-v4`；源码和真实
运行元数据均只使用这两个规定型号，故无需把邮件预批设为必要条件。Gate 2 的剩余
缺口是固定 Commit、正式镜像 digest 与实际部署映射，仍不勾选 Gate 2。

## Task 3：固定、打包与本机验收（10 月 11–15 日）

**Files / interfaces:** `Dockerfile`、`.dockerignore`、`pyproject.toml`、`.env.example`、`deployments/docker-compose*.yml`、`deployments/Caddyfile`、`alembic/`、`src/masm/agents/prompts/`、`tests/`、发布记录。候选对外仍为同步 Add、Search、Health；新版本标识不可静默移动旧标签。

- [ ] 逐文件区分当前工作树原有改动、必要候选改动与本机实验/报告；保留所有原文件，不做破坏式清理。形成待纳入 Commit 的显式文件列表，排除 `.env*` 私密配置、`.superpowers/`、原始样本/图片、运行 JSON/TSV、账单与 DB 产物。
- [ ] 核查 `.dockerignore` 与实际构建上下文，修掉 `.worktrees/`、`.superpowers/`、`.env.local` 可进入上下文的风险；以经审计的 **干净固定提交导出** 构建，不用脏仓库根目录直接打正式镜像。当前窄 `COPY` 不能替代上下文审计。
- [ ] 检查候选 Prompt wheel/package 资源、运行依赖、**候选源码自己的 Alembic head**、入口/Health、无密钥日志、卷/端口/网络隔离和 Caddy 静态语法。当前 v1.1 为 `0005`，本机 `submission-rc2` 为 `0003`；分别在独立库验证，不能把迁移到 `0005` 当作 v1.0 标签的要求，也不得在未做备份/适配验证时让两版共用生产库。用占位值验证配置，不把占位验证说成公网通畅。
- [ ] 在候选固定后，以隔离 `masm_test` 重跑全量 `pytest -q`、`ruff check .`、`mypy src`、`git diff --check`、契约/数据库/媒体定向测试、敏感文件/高风险字串扫描、构建包内容检查；保存命令、退出码、警告及产物哈希。重复测试后做精确清理核对。
- [ ] 形成不可变候选清单：Commit SHA、tree hash、镜像 digest、Prompt/模型/非秘密配置摘要、Dockerfile/Compose 版本与回滚点。新修复产生新 Commit/镜像/官网 Version，不能悄悄挪动 `submission-rc2`。
- [ ] **Gate 3:** 任何测试失败、无法解释的构建内容或源码/镜像不一致均停止冻结；不以旧的 602 项结果替代候选新回归。公开推送另需用户明确批准。

## Task 4：经授权的线上安全验证与容量声明（10 月 16–24 日）

**Files / interfaces:** `deployments/`、发布/回滚记录；公开 GET Health、POST Add、POST Search 及 `X-Api-Key`。自托管 Add 必须在 HTTP 200 返回时已经可立即 Search；`request_id` 原样回显，`user_id` 隔离。

- [ ] **授权关口 A：只读审计。** 仅检查现有服务/卷/镜像/健康/资源余量与域名证书，记录脱敏摘要；不能将只读授权理解为可备份或重启。
- [ ] **授权关口 B：备份和恢复。** 获单独授权后对 v1.0 DB 与资产做一致性备份、哈希验证和隔离恢复演练；只在确认恢复可用后考虑部署。备份位置/保留期/回滚负责人写入记录，不碰 `down -v`。
- [ ] **授权关口 C：部署。** 若选 v1.1，使用独立 DB、资产卷、服务/Key/域名，先静态校验再并行启动；若选 v1.0，按固定镜像核对并尽量避免无必要重建。验证 HTTPS/Health/401、文本/图文/纯图 Add/Search、同 request_id 重试、立即可检索、重启持久、跨用户隔离和原 v1.0 健康；异常只撤本次新服务并保留诊断数据。
- [ ] **授权关口 D：容量。** 用户另行批准后在隔离负载环境递增测试 Add/Search 并发，记录每档错误率、P95、资源、Provider 429/503、超时与数据清理。验证 `top_k≤100`、图片 10 MiB、Add 30 MiB、Search 响应 30 MiB 等官方上限的实际行为；只申报验证过的并发数，不把 16/16 当官方硬门槛。
- [ ] **Gate 4:** 线上可用、可回滚、旧服务无退化；容量和费用有可审计数字。任一失败都不推进官网 Smoke。

## Task 5：官网材料、Smoke、Full 与收尾（10 月 24 日–11 月 04 日）

**Files / interfaces:** 官网表单及本地脱敏提交记录。官网公开仓库固定 Commit、Memory System Version、镜像 digest、运行端点必须一一对应；Eval Key 与 Memory System Key 分开保管，绝不放入仓库、URL、截图或聊天。

- [ ] **授权关口 E：公开推送与官网申请。** 用户批准后推送固定 Commit/必要材料；核对仓库对外可读及许可证/第三方归属。按官网表单填写多模态开源赛道、系统/Version、固定 Commit、Add/Search URL、鉴权方式、如实容量和模型说明，提交后保存脱敏回执。Eval Key 的申请/审批状态单列；官网说明获批后通常次日北京时间 19:00 前发放，不以此保证实际时间。
- [ ] **授权关口 F：官方 Smoke。** 版本与线上镜像核对后，获单独批准才运行；记录 Run ID、时间、脱敏错误和配额。Smoke 失败先定位契约/部署原因；修复后用新的可追溯固定版本，不在已绑定 Version 下偷换镜像。官网 Smoke 约每小时 1 次、每赛道至多 30 次，提交前再次核对。
- [ ] **授权关口 G：官方 Full。** Smoke 与所有 Gate 通过、用户审阅最终费用/容量/版本证据后，获单独批准启动；建议不晚于 10 月 30 日，给 0.5–2 天运行和故障处理留余量。开始前检查本赛道 Full 已使用次数、Eval Key 与材料截止状态；Full 结果不佳不能立即重试，本赛期按一次机会处理。
- [ ] 运行中只监测任务阶段、端点健康、预算停止线、脱敏错误；平台失败按官方允许的断点续跑/申诉处理，不换 Key 规避额度。Full 发起不等于有效提交；需完成评测并核对材料/版本/合规审核状态。
- [ ] 结束后按官网现行规则，在任务完成 **30 天内**删除收到的评测数据及派生副本，除非平台书面批准其他保留期；不得将其用于训练、微调、产品分析、数据集重建或传播。保留不含原文/图片/密钥的 Commit、镜像、Run ID、费用口径、清理与结果审计摘要。清理时再核对当时官网规则，避免误删申诉所需的非敏感证据。
- [ ] **Gate 5:** 只有官网显示 Full 完成且材料及合规审核通过，才报告“有效提交”；否则逐项报告已完成和仍待平台确认。

## 3. 每次阶段汇报格式

每一步记录：`操作/时间/环境与版本/命令或官方动作/退出码或平台状态/证据位置/已验证结论/未验证事项/是否触及授权边界/下一门槛`。数据为小样本时写明 `n`，费用区分美元与人民币、估算与实付；历史 OpenAI MASM 专属费用始终 `unavailable/not_reconciled`。不在公开材料或日志中展示密钥、原始个人记忆或真实图片。

## 4. 本计划的执行起点

本轮仅制定与审阅计划，不运行 Provider、容量或官方评测，也不修改云服务器/生产库、不推送代码。计划获认可后先执行 Task 1 的只读证据账本与本机离线核对；到每个外部关口再分别请求对应授权。若用户希望直接采用 v1.0 保底，也仍须完成 Task 2–5 的身份、合规、打包、部署与官方门槛。
