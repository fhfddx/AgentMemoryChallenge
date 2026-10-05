# MASM 最终候选证据账本（2026-10-05）

用途：为最终选版建立同口径事实，不把源码、本机档位与公网镜像混作一个版本。本文只记录聚合证据、来源和未验证项，不保存密钥、请求原文或图片。执行计划见 `docs/superpowers/plans/2026-10-05-masm-final-submission-execution-v2.md`。

## 1. 版本与样本口径

| 名称 | 实际对象 | 可用于什么判断 | 不可用于什么判断 |
| --- | --- | --- | --- |
| B0 v1.0 | 早期 `scripts/metered_holdout.py --version v1.0` 默认 `official-baseline` 档位 | 复现报告第 1–38 节的旧样本 | 不能直接代表公网 MASM v1.0 或与 v1.1 同档位因果比较 |
| 本机 MASM v1.0 | 主仓库 `af9173f939b652443be4f27ef438bb1d6e79e8cf` 源码，显式 `--runtime-profile official-masm` | 报告第 40–44 节的同档位源码对照 | 不能证明公网运行镜像与该源码相同 |
| 本机 MASM v1.1 | linked worktree `codex/masm-v11`，HEAD `1c7b26a8a0e924cb9343a5afdc47185b25b0841b` 加当前未提交代码，`official-masm`，实验融合默认关闭 | 当前本机回归与受限诊断 | HEAD SHA 本身不代表未提交代码的固定版本 |
| 公网 v1.0 | 2026-09-30 记录为 `official-masm`，当时镜像由 `fe540e0` 构建，镜像 ID 见公网验证文档；本机 `submission-rc2` 标签指向 `db7d75a95c2c6a119425ed1b378023297aac03ce` | 历史公网功能/小并发记录 | 2026-10-05 当前线上 digest、Commit、配置映射未获授权核对，不能称与 `submission-rc2` 对齐 |

本机标签 `submission-rc2` 的迁移头是 `0003`；当前 v1.1 工作树的迁移头是
`0005`。两者必须按各自固定源码和隔离数据库核验，不能把 v1.1 的 0005 测试库
或其 602 项回归当作 v1.0 标签的验证，也不能由迁移头不同直接推断线上故障。

## 2. 已完成诊断的证据强度

| 记录与来源 | 样本／运行 | 质量和延迟观察 | 失败、用量与清理 | 结论边界 |
| --- | --- | --- | --- | --- |
| 10-03 合成七类，`masm-v11-formal-model-pilot.md` | 两版各 7 Add；图文、纯图各 1 例 | 仅双消息多事实原子覆盖 0→1；图片只按非空计分 | 14 个 Add 清理清单完成；Provider usage 不可得 | 自编、B0 与 v1.1 非同档位，不证明真实图片语义 |
| 10-05 同档位 `primary` 六类，报告 §40–41 | 两版各 6 Add/6 Search，项目自编文本，按序运行 | Add 均值 4875.58→3154.34 ms；P95 5679.08→4067.80 ms；Search P95 1241.01→1246.55 ms；标准 Recall 均 1；仅自编多事实原子 0→1 | LLM 21→16、Embedding 18→13；失败/usage 缺失均 0；两版各 6 条精确清理，运行前缀四表 0/0/0/0、资产 0；合计原价估算 $0.00678015 + ¥0.000371 | 仅本机源码、各 n=6；不能证明独立质量增益或容量 |
| 10-05 LongMemEval Oracle 摘录，报告 §32–34 | 来源固定修订 `98d7416...`、MIT；仅 B0 基线 6 Add/3 Search | 6/6 字面标记及原子证据达量尺上限，因此未付费跑 v1.1 | 失败/usage 缺失 0；运行前缀四表及资产 0 | 删去干扰历史；非同档位两版对照、非官方完整历史 |
| 10-05 Oracle 跨案例干扰，报告 §43–44 | 两版同 `official-masm`，各 8 Add/3 Search；2 正题取自固定 Oracle 发言，混排和护照拒答题由项目构造 | 正题两版 Recall@10/原子覆盖均 1；严格空证据两版均 0（各返回 2 条无关证据）；Add 均值 5130.06→4432.52 ms，P95 6793.36→6636.58 ms | LLM 24→21、Embedding 19→16；失败/usage 缺失均 0；各 8 条精确清理，四表 0/0/0/0、资产目录不存在；合计原价估算 $0.0097278 + ¥0.0013995 | 无 v1.1 独立证据增益；不是官方样本，也不能将 Search 误召回直接称最终回答泄漏 |
| 10-05 独立 CC0 真实图片，报告 §45 | 两版同 `official-masm`，各 3 Add/3 Search；3 张事前固定且逐图核许可/哈希的 Wikimedia Commons CC0 照片 | v1.0 三题标记 Recall@10 为 0/3，v1.1 为 3/3；Add P95 19778.44→24235.25 ms（+22.5%，低于事前 +25% 上限），Search P95 1859.47→1324.58 ms | LLM 14→11、Embedding 12→9、图片尝试均 6；失败/usage 缺失均 0；各 3 条清理，四表和资产 0；合计原价估算 $0.05228925 + ¥0.000505 | n=3，仅证明本机 Search 证据的图像语义字面覆盖；达到事前最小差值，支持 Gate 1 `v1.1 go`，不替代官方分数/容量/合规/线上验证 |
| 10-05 合成媒体计量，报告 §36–38 | B0 v1.0 与 MASM v1.1 各 2 例，图文和纯图 | 两版图片路径走通、Search 非空；无真实图像语义判分 | 带图 LLM HTTP 3/5；清理后四表/资产 0；原价为整次 LLM 而非图片专属金额 | 非同档位版本比较、非独立媒体质量或图片专属费用 |

所有新运行费用是按公开原价估算，非供应商实付对账。历史 OpenAI MASM 专属金额按用户决定不再追查，状态保持 `unavailable/not_reconciled`，绝不写成零。上述各运行的清理结论只限对应前缀与当时临时库，不表示任意旧数据均为零。

## 3. 本轮新鲜本机核对（2026-10-05）

- 工作树为现有 linked worktree `codex/masm-v11`；原有已跟踪/未跟踪改动未清理或重置。`Settings.fused_empty_history_text=False`，环境开关只有显式 `MASM_FUSED_EMPTY_HISTORY_TEXT=1` 才启用；本轮未启用。
- 系统 Python 因未装 `fastapi`，首次测试停在 `conftest.py` 导入，退出码 1，**没有执行用例**。改用项目根 `.venv/Scripts/python.exe` 后，相关单元测试 **28 passed、2 warnings**；时间/计量集成测试 **20 passed、2 warnings**；完整 `pytest -q` **602 passed、74 warnings、退出码 0**。警告仍是现有依赖弃用警告；这些测试不证明正式模型回答正确。
- 本轮独立 `masm_test` 容器仅映射 `127.0.0.1:5433`，数据库目录 `tmpfs`、`AutoRemove=true`，迁移至 Alembic `0005`。停库前 `synthetic-holdout-%` 在 `request_ledger/source_messages/memories/assets` 为 **0/0/0/0**；`fused-%` 测试夹具为 **12/12/25/0**，故不宣称全库零残留。确认容器 ID/配置后仅停止本轮临时容器；原有四个业务容器继续健康。
- `docs/competition/masm-v11-temporal-regression-matrix.md` 的 T1–T7 与 `tests/integration/test_fused_text_pipeline.py` 对应：规范数字日、文字/数字双日、绝对+相对日、标识符八位数字、文字日、无日、跨会话改期及同用户历史。当前 Fake 测试证明**默认关闭融合时旧时序结果被传入并持久化**，不证明真实 LLM 能算对这些日期；实验融合中 T2/T3 风险仍在，继续关闭。

## 4. 新独立样本的只读可得性与停止线

- 已用过的 [LongMemEval cleaned Oracle](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) 摘录与跨案例组合都已达到或失去辨识力，不再重跑同型付费诊断。
- [Mem-Gallery 数据卡](https://huggingface.co/datasets/Ethan-Bei/Mem-Gallery)公开标注 MIT，含多会话对话/图片/问答；[作者论文附录 A.2.2](https://arxiv.org/html/2601.03515)介绍了公开图片来源与题目证据回合。然而，2026-10-05 复核的[赛事官方参赛说明](https://agentmemoryleaderboard.ai/rules)把 **Mem-Gallery 明列为多模态基准之一**。因此它不能作为“独立于比赛基准”的 v1.1 选版留出集，也不应被用于针对官网题型调参；本轮未下载、运行或据此修改代码。另一个候选 MemLens 的[项目说明](https://github.com/xrenaf/MEMLENS)明确图片仍遵从各原来源许可，未逐图核清前不选用图片。当前仍缺一个许可清楚、可判分且不在官方套件中的新独立媒体/事件时间样本；没有付费运行协议。
- 另检索到与官网套件不同的 [H2HMem 数据卡](https://huggingface.co/datasets/varib/H2HMEM)，但数据卡标 `MIT`，[作者论文](https://arxiv.org/html/2606.09461)却写 `CC BY 4.0` 且“仅供研究”，许可口径尚不一致；在澄清前不使用它的图片。ConvoMem 数据卡标 CC-BY-NC-4.0，也暂不作为有奖金赛事的用量对照来源。两者均未下载或运行。
- 后续找到三张与官网套件无关、逐图标为 CC0 的 Wikimedia Commons 真实照片；已在看结果前固定图片/清单哈希、问题、原子标记、同档位、最小差值、延迟和请求硬限，结果见报告 §45。该一次性 n=3 配对完成后停止新增付费样本，不按结果换图或改标记。

## 5. 当前选版判断

计划 Task 1 已完成：独立 CC0 真实图片配对中 v1.1 为 3/3、v1.0 为 0/3，且通过事前延迟、安全、usage 与清理条件，故 **Gate 1 判定为 `v1.1 go`**，停止新增付费样本并把 v1.1 作为唯一的本机最终候选。**已验证：**当前代码回归、默认融合关闭、同档位文本/媒体小样本、独立图片字面语义增益、对应运行零残留，以及按官网现行文字完成的模型/内部架构合规映射。**待验证：**真实模型事件时间完整性、固定 Commit/镜像 digest、公网部署与回滚、可申报容量及官方 Smoke/Full。Gate 2 仍待版本/镜像来源完成，Gate 3–5 仍未通过。

## 6. 官方当前规则逐项对照与 v1.0 标签本机验证（2026-10-05）

以 2026-10-05 重新读取的[官方参赛说明及内嵌 API 指南](https://agentmemoryleaderboard.ai/rules)
和[赛事 FAQ](https://agentmemoryleaderboard.ai/competition/)为准；本地计划与旧记录若有冲突，
以官网当前规则为准。**官网要求**：开源方法仍须自托管 Add/Search、公开仓库固定 Commit；
Add 同步写入，HTTP 200 时相关记忆须立即可检索；Search 按完全相同的 `user_id`
隔离、只返回记忆证据，响应为 `{"data": [...]}` 且不得超过 `top_k`；
多模态只接受有序内容数组中的内联 Base64 图片，不接受远程图片 URL；单图解码
10 MiB、单次 Add 图片合计 30 MiB、Search 响应图片合计 30 MiB；
学术榜 FAQ 指定 LLM `gpt-4o-mini`、Embedding `text-embedding-v4`、Reranker 不限。
官网还规定收到的评测数据和派生副本仅用于当次任务，不得用于训练、微调、
产品分析、数据集重建或传播；任务完成后 30 天内删除，除非平台书面批准延长。

**本机静态核对**：当前 `src/masm/schemas/api.py`、`content.py`、`api/routes.py`
分别有必填回显字段、`top_k<=100`、非空证据/`data` 包装、内联 data URI 校验、
`/health` 及同步路由；`Settings` 的默认媒体上限和模型名与上述官网值一致。
`src/masm/services/search_service.py` 按请求 `user_id` 检索并过滤候选，只生成证据响应；
这些是源码/测试层核对，不是线上接口或官方 Smoke 证明。官网文档同时明确：
平台不限定数据库、索引或内部架构；多模态“原图优先，caption 兼容”，支持图片
的系统处理原图；学术榜所有 LLM 组件使用 `gpt-4o-mini`、Embedding 使用
`text-embedding-v4`，Reranker 不限。当前正式档位把所有 LLM 调用锁定为前者，
所有向量调用锁定为后者，图片先由前者读取原图并生成可观察描述，再由后者嵌入，
Reranker 为词法实现。因此该路径符合**当前官网公开规则**，无需把“另获架构预批”
作为提交前置条件；但最终有效提交仍须通过主办方版本、结果与合规复核。

**标签隔离验证**：从固定 `submission-rc2` Git 树导出干净文件，独立的
`masm_test` PostgreSQL/pgvector 16 容器只绑定 `127.0.0.1:5433`，
数据目录为 `tmpfs`。在该导出目录运行 Alembic `upgrade head`、`current`，
退出码均为 0，结果为 `0003 (head)`；再用项目 `.venv` 在该导出运行
`pytest -q`，**463 passed、74 warnings、退出码 0**。进程退出时 pytest
另报告系统临时目录 `pytest-current` 链接的 WinError 5，未改变测试退出码；
这是需要排查的环境收尾警告，不应写成完全无警告。停库前该隔离库全局
`users/source_messages/memories/assets` 为 `140/217/246/50`，主要是测试夹具，
**不是零残留**；确认容器名、`tmpfs` 与回环端口后，仅停止自动删除的本轮容器，
四个原有业务容器仍健康。本机验证不证明公网镜像、生产库迁移或真实 Provider。

**标签打包预检**：首次用项目 `.venv` 的 `pip wheel --no-build-isolation`
退出码 1，原因是该虚拟环境没有安装 `setuptools`/`wheel`，而非源码编译报错；
未修改或联网安装该环境。改用本机已有 Anaconda Python 3.13、
`setuptools 72.1.0`、`wheel 0.45.1`，在同一干净导出运行
`pip wheel --no-deps --no-build-isolation` 退出码 0，生成
`masm-0.1.0-py3-none-any.whl`，SHA-256
`096860bd047acdfeedf2641e58b9f47ed8280952bab75eaf3fcffc5aa8ba9c06`。
只读列出 wheel 条目可见三份 Prompt（curator/perception/temporal），
未列出 `.env`、测试或 `.superpowers` 路径。此 wheel 是**本机预检产物**，
不是固定线上镜像；未在干净运行环境安装或做镜像 digest 验证。

**本机镜像预检**：为避免上述 wheel 构建产生的 `src/masm.egg-info`
污染镜像上下文，重新从 `submission-rc2` 导出另一份 148 文件的干净目录；
导出中没有 `.env` 私密文件，只有仓库示例 `.env.example`，无 egg-info。
从该目录执行 `docker build --pull=false --progress=plain`，退出码 0，
上下文传输量 292.33 kB；本机镜像标签
`masm-rc2-local-preflight:20261005` 的 manifest-list SHA-256 为
`ed975bf5a49253b5d9ab317643276abde1aa40ca081704fef25e1fa88d28ff37`。
用 `--network none`、不启动 API 的容器检查 `/app`：三份 Prompt 与
Alembic `0001`–`0003` 均存在，`/app/.env` 和 `/app/tests` 不存在。
Docker 构建期间 `pip` 下载了满足宽版本范围的当期依赖，故此预检不证明未来
重建 bit-for-bit 相同；也没有在该镜像上启动 Add/Search 或核对线上 digest。

**当前未通过**：官网 Version 绑定、线上代码↔镜像 digest↔配置、HTTPS/Auth、
媒体方法的具体合规、独立质量、可申报容量及官方 Smoke/Full 均未核验；
不因 463/602 项本机测试通过而勾选 Gate 2–5。

## 7. 冻结前独立审查修复（2026-10-05）

独立只读审查无 Critical，3 个 Important 经 TDD 修复：拒绝测试库 URL 的全部
query 参数；在计量器记录越过尝试上限，并由 Add/Search 评估边界终止；拒绝
外部媒体清单/图片符号链接并验证真实路径包含关系。定向测试先分别失败，再通过；
计量单元文件最终 **68 passed**。在新的本机随机端口 pgvector/pg16 `tmpfs`
临时库升级到 `0005` 后，完整回归为 **613 passed、74 warnings、退出码 0**，
Ruff、mypy 52 个源码文件、差异检查均退出 0。pytest 退出后的已知 WinError 5
仍存在。全套测试夹具使删库前四表总数为 `291/291/600/50`，不宣称全库零；
整个命名临时库容器已精确删除，四个原有容器健康。

审查的两个 Minor 延后：价格来源字段的纯空白校验，以及将旧文档中已被后文
覆盖的谨慎表述做编辑性合并。它们不改变运行时契约、安全边界或当前选版。
固定 Commit、tree hash、从该 Commit 干净导出的 wheel/镜像、镜像 digest 与
容器契约仍未完成，不能据本节宣称 Gate 3 通过。
