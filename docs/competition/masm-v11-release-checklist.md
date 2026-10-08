# MASM v1.1 独立发布清单（待批准执行）

本清单是发布门槛，不是已执行记录。当前已完成本地代码、假模型合成对照、配置验证，
以及 7 类正式模型本机小样本对照；后者也不是官方成绩。后续同 `official-masm`
源码档位的六例配对显示 v1.1 本机写入较快，但不足以证明公网镜像、独立质量、
容量或最终成本，因此尚未通过发布门槛。线上 v1.0 的实际镜像与本机
`submission-rc2` 标签是否一致仍待核对；其数据库、资产卷和现有域名
`agentmemorydev.icu` 必须保持原状。是否上线新版本及是否使用官方 Smoke，需在审阅正式
模型小样本、容量、时延和成本后由用户决定。
**口径更正：**历史脚本标为 v1.0 的本机计量使用 `official-baseline`（B0），
而公网部署记录是 `official-masm`。旧两版延迟差值不构成线上 v1.0 MASM
对 v1.1 的同档位比较；新的本机同档位配对见结果报告第 41 节。不得凭
任一六例样本冻结版本。

## 1. 上线前核对

- [ ] 确认分支提交 SHA、测试总数、迁移头 `0005`、正式配置
  `official-masm / gpt-4o-mini / text-embedding-v4 / 1024`；记录 v1.0 当前 SHA、镜像
  digest 和配置摘要，不把密钥复制进记录。
- [ ] 对 v1.0 PostgreSQL 和对象卷做同一时点备份、校验 SHA256，并在隔离环境演练恢复；
  v1.1 使用另一个数据库和资产卷，绝不把迁移直接指向 v1.0 数据库。
- [ ] 以只含占位值的环境运行 `docker compose config -q`、
  `docker compose -p masm-v11 -f deployments/docker-compose.v11.yml config -q`，并验证
  Caddyfile。检查解析后的 v1.0 路由仍为 `api:8000`，v1.1 为 `masm-v11-api:8000`。
- [ ] 准备仅供 v1.1 使用的 `MASM_V11_POSTGRES_PASSWORD`、`MASM_V11_API_KEYS`、
  `MASM_V11_RUNTIME_PROFILE=official-masm`；沿用已合规的 LLM/Embedding 供应商配置，
  但不要在命令、日志或文档中打印密钥。前两项在 Compose 中是必填项、没有默认值；
  数据库口令进入连接 URL，需使用 URL 安全随机字符（字母、数字、`-`、`_`）。
- [ ] 给 `v11.agentmemorydev.icu` 添加指向服务器公网 IP 的 DNS，待公网解析成功。
  新域名仅用于并行候选，旧域名不切换。

## 2. 隔离启动顺序

1. 确认 `masm-edge` 网络不存在或属于本项目；必要时创建一次。旧 Compose 的 Caddy
   与新 Compose 的 API 通过该外部网络通信，新 Postgres 不接入边缘网络。
2. 先启动 `masm-v11` Compose 的 API/Postgres。它只在宿主机 `127.0.0.1:8002`
   暴露调试端口，Postgres 不暴露端口；等待迁移成功和容器 Health healthy。
3. 对 `http://127.0.0.1:8002/health` 做本机检查，再仅重建旧项目的 Caddy 容器以加载
   第二站点及边缘网络。不要重建或替换 v1.0 API/Postgres。
4. 从外网分别核对 `https://agentmemorydev.icu/health` 与
   `https://v11.agentmemorydev.icu/health`，均应返回 `{"status":"ok"}`；检查两张证书
   的域名和有效期。v1.0 原域名必须始终可用。

## 3. 正式模型小样本与容量门槛

- [ ] v1.1 独立 Key 做文本、图文、纯图片 Add/Search/重启持久性 Smoke，核对真正的
  消息来源与上下文、HTTP 401、幂等重试和运行级删除；测试运行的数据库行、向量、
  资产对象全部清理，不能碰 v1.0 或其他用户。
- [ ] 在 v1.0/v1.1 使用同一预注册小样本与正式模型重跑对照，分别报告标准
  Recall@10/100、原子证据覆盖率、返回条数、P95 延迟、响应字节、Embedding 输入量、
  LLM/感知调用数和费用；未观测到的指标标为 `unavailable`，不估算。
- [ ] 验证并发 Add 16、Search 16 的错误率、P95 和内存/CPU；复核 30 MiB 响应上限、
  Provider 最多 64 条重排输入、最终 Top K 100。
- [ ] 若覆盖收益不足、延迟/成本过高、图片感知增加 LLM 调用，或存在数据泄露/跨用户
  问题，停止发布并回到本地，不创建新的官方版本。

## 4. 回滚与官方提交

- [ ] 回滚只撤下 v1.1 站点或停止 `masm-v11` API；保留 v1.1 数据卷以便诊断和恢复，
  不使用 `down -v`。旧域名和旧 API/Postgres 不应受到回滚影响。
- [ ] 只有用户审阅正式模型对照、成本与容量记录并明确同意后，才在官网添加 v1.1
  Version、填写独立 URL/Key 并运行一次官方 Smoke。当前不进行此步，也不上传密钥。
- [ ] 上线后检查 HTTPS、401、容器 Health、日志轮转 `10m × 5`、磁盘/证书到期，
  以及备份可恢复性；发生异常按上一条回滚。

本机合成对照和局限见 `masm-v11-local-synthetic-report.md`；正式模型本机小样本及
暂缓发布的依据见 `masm-v11-formal-model-pilot.md`。公开独立来源的 3 案例文本
摘录对照见 `masm-v11-metered-holdout-report.md` 第 12 节；该轮两版字面检索覆盖均为
3/3，但 v1.1 有一次失败 HTTP 尝试及 usage 缺失。百炼两个测试窗口的应付额
已在用量层面对齐；OpenAI 只有整日成本导出、没有本次测试专属金额，详见同报告
第 13 节，不改变上述未勾选的发布门槛。

## 5. 本机静态预检记录（2026-10-04）

在现有 `codex/masm-v11` 工作树中，用进程级占位值执行
`docker compose -p masm-v11 -f deployments/docker-compose.v11.yml config -q`，
退出码为 0。仅从解析结果读取非敏感结构：API 端口为
`127.0.0.1:8002 -> 8000`；Postgres 无 `ports` 属性、只连默认网络；
API 连默认网络和外部 `masm-edge`；卷名为 `masm-v11-asset-data` 与
`masm-v11-postgres-data`；运行档位 `official-masm`，模型及维度为
`gpt-4o-mini / text-embedding-v4 / 1024`。没有启动容器或连接服务器。

只读查看 Caddyfile 的旧站点目标为 `api:8000`，新站点目标为
`masm-v11-api:8000`；本机没有 Caddy 可执行文件，未做语法验证。旧 Compose
也未在本轮重新运行静态检查。因此第 1 节的整项配置门槛仍保持未勾选；DNS、
备份恢复、独立 Key、图片成本、并发容量及官方 Smoke 同样未验证。
在这些条件以及 OpenAI 专属费用、v1.1 写入时延和重试影响得到审阅前，继续暂缓
部署和官方提交。

## 6. 本机静态与测试续验（2026-10-04）

第 5 节“Caddy 语法与旧 Compose 尚未重新验证”是当时状态，现由本节更新：
两版 Compose 在进程级占位值下 `config -q` 均通过；本机已有 Caddy 镜像
在禁止拉取和联网的临时容器中对当前 Caddyfile 执行 `validate`，结果有效。
源码和本机 `masm_test` 的迁移头均为 `0005`；完整测试 `550 passed`，
Ruff 全仓通过。测试库已关闭。具体证据与限制见
`masm-v11-metered-holdout-report.md` 第 19 节。

这些检查仍不是第 1 节整项上线门槛：线上 v1.0 的 SHA/镜像摘要、
备份恢复、DNS/证书、独立 Key、图片成本、并发容量及官方 Smoke 未验证，
相应复选框继续保持未勾选；不能据此部署或提交新版本。

## 7. 空历史写入优化后的本机观测（2026-10-04）

现有 v1.1 工作树在无同用户历史候选时跳过不会产生可执行关系动作的
Curator 模型调用；有历史时仍调用。完整测试 `551 passed`，同源公开文本
3 案例复验中 v1.1 LLM HTTP 从上轮 11 次降为 8 次，Add 平均耗时从
5127.38 ms 变为 3683.74 ms，两轮检索证据覆盖均为 3/3。该比较受网络与
模型波动影响，且优化后仍明显慢于同期 v1.0；实际账单未核对。
详细结果和清理记录见 `masm-v11-metered-holdout-report.md` 第 20 节。
另用固定六类合成模板复核，第 21 节记录 v1.1 多事实字面原子覆盖
仍为 1、六类标准 Recall 均命中；六类小样本不是盲测，也不能弥补
实际账单、图片与并发证据缺口。
因此本清单的时延/成本、图片、容量和官方门槛继续保持未勾选。

## 8. 账单追查收口与下一项本机优化（2026-10-04）

用户决定不再追查历史测试窗口的 OpenAI MASM 专属金额。它保持
`unavailable / not_reconciled`，不视为零费用，也不据此勾选成本门槛；
不再为补账单追加付费重跑。第 5–7 节中“待核对 OpenAI 专属费用”是
此前状态，以本节决定为准。仍需评估可观察的 Provider 调用量、时延、
图片路径和正式发布所需的其余门槛。

本机又消除空历史 Add 的一次无候选文本 Embedding 请求；有同用户历史
时仍按原流程召回。失败先行测试与完整测试 `553 passed`，但未以真实
Provider 复测，因此不声称新的延迟或金额收益。时延/成本、图片、容量、
独立环境和官方门槛保持未勾选。

## 9. 写入延迟根因与传输层收尾（2026-10-04）

普通应用路径的 LLM/文本 Embedding Provider 已改为复用自身 HTTP Client，
由应用生命周期关闭；测试 `557 passed`、Ruff 与改动源码 mypy 通过。
但既有计量小样本已注入可复用 Client，故其约 3.5 秒 v1.1 Add 均值
主要仍是感知与时序串行模型等待，不能据本次传输改动宣称达成时延门槛。
真正减少模型阶段需另行验证时序信息与检索质量，不在本节直接删减。

## 10. 空历史文本融合的本机试验（2026-10-04）

新增默认关闭的 `MASM_FUSED_EMPTY_HISTORY_TEXT` 试验开关，仅纯文本且无
同用户历史时合并感知与写入所需时间字段。固定公开文本 3 例中，旧路径
与收窄融合的 Add 均值为 3846.25/2508.64 ms，字面 Recall 均 3/3，
LLM HTTP 为 8/5 次；两条独立非敏感时间探针分别通过。完整测试
`570 passed`、Ruff/mypy 通过，三轮测试记录和资产零残留。详细限制见
结果报告第 24 节。样本过小、跨轮网络条件不同、没有图片及大范围时间
质量/并发证据，故不开启默认值，也不勾选时延、成本或发布门槛。

## 11. 六类复核未执行（2026-10-04）

拟用固定六类文本补验融合路径，但本机既有 `masm_test` 集群启动时
PostgreSQL 绑定 localhost:5433 返回 `Permission denied`；没有改端口
或使用其他数据库继续运行，也没有新增 Provider 请求。独立代码审查因
工具用量限制未返回结果。详见结果报告第 25 节；第 10 节的三例观测
不扩展为六类结论，相关发布门槛继续未勾选。

## 12. Docker 本机六类复核与融合时间风险（2026-10-04）

第 11 节的数据库阻塞已随 Docker 启动而解除；只使用新建的隔离临时
`masm_test` 容器，旧业务容器未动。两组六类受限对照发现融合路径会把
合成标识符中的八位数字误作事件日期，且替代模板未稳定提速。
已加入日期忠实性校验和紧凑日期预检，开关仍默认关闭。
最终完整测试 `576 passed`、Ruff/mypy 通过，运行标记对应的临时库四表与资产零残留，
临时容器已停止并自动移除；预检后真实时延及更广泛时间质量尚未验证。
详见结果报告第 26 节。时延/质量、独立 Key、图片、容量及官方门槛
仍不勾选，不能部署或提交新版本。

## 13. 融合入口时间语义保护（2026-10-05）

针对非 ISO 日期可能被融合空时间静默丢失的问题，现仅让恰有一处可核验
独立 ISO/斜杠日期的纯文本无历史输入尝试融合；无日期、非 ISO 日期、
两处规范数字日期及紧凑日期候选直接保留旧时序路径。完整测试 `579 passed`，
Ruff/mypy 通过；预检后无新的正式 Provider 延迟或质量对照。
默认开关仍关闭。旧三例实验性提速不代表当前代码，时延、时间质量、
图片、独立环境、容量及官方门槛继续不勾选。详见结果报告第 27 节。

## 14. 只读审查后的日期边界（2026-10-05）

进一步排除唯一规范日期之外含其它数字的文本，并严格限制两位月/日与
一致的日期分隔符；含额外数字的混合格式双日期及非规范写法改走旧路径。
`max_history=0` 仍按原设计表示本次不提供历史候选，不保证库中无记忆。
完整测试 `583 passed`，Ruff/mypy 通过，但未重跑正式 Provider 时延。
详细结果见报告第 28 节；所有发布门槛仍未勾选。

## 15. 自然语言时间语义仍未达标（2026-10-05）

无额外数字不等于无额外时间表达；复审给出的文字日期/相对日期反例
仍可进入融合。当前只读审查与 583 个测试不能证明该实验路径质量。
停止叠加日期正则补丁，维持默认关闭；重新设计并取得逐条时间质量
证据前，不勾选时延、质量或发布门槛。详见结果报告第 29 节。

## 16. 最终提交计划第一阶段的本机回归（2026-10-05）

根据预注册 T1–T7 时间矩阵，新增 7 个 Fake Provider/数据库断言，检验
融合默认关闭时旧时序结果、跨会话同用户历史与入库字段。全量测试
`590 passed`、Ruff/mypy/差异检查通过；占位值 Compose 配置未传递融合开关。
这些断言不证明正式模型能正确解释自然语言多时间表达；实验开关显式开启
时仍有第 29 节反例。当前正式 Provider 质量、时延、图片计量和容量证据
没有新增，发布门槛继续不勾选。详见结果报告第 30 节。

## 17. 第二阶段计量准备与严格拒答缺口（2026-10-05）

独立来源文本加载器现能明确表示“有干扰历史但应返回空证据”的案例，
图片相关 LLM HTTP 尝试可在脱敏计量中单独计数。Fake HTTP 单元测试
54 项通过；本机数据库集成测试 3 项通过，测试运行四表残留为 0。
在新拒答诊断中，v1.0 对无关笔记历史返回非空证据，严格空证据得分 0；
这与此前空用户拒答通过不矛盾，也不能直接推断最终答案错误。
尚无 v1.1 同源结果、独立分层样本、媒体真实计量或费用归属，质量/时延/
成本/容量及官方门槛均不勾选。详见结果报告第 31 节。
增补后全仓回归 `597 passed`、74 条既有警告，Ruff/mypy 通过；本轮临时库
相关运行前缀四表残留均为 0，容器已自动移除，原有四容器仍健康。
另冻结了三类 LongMemEval Oracle 文本证据摘录与 SHA-256，当时尚未发起
付费对照；它不含完整干扰历史和媒体，不能独自放行 Gate 2。
后续基线诊断见本清单第 18 节及报告第 32–34 节。

## 18. Oracle 摘录基线诊断与提前停止（2026-10-05）

事前设定两版尝试硬限和质量/成本停止线后，只完成 v1.0：三类 6/6
字面证据均在 Top 10 命中，原子覆盖也满分；6 Add、3 Search，相关
四表与资产残留为 0。该摘录无法再展示预定的正向标记收益，因此没有
对它付费运行 v1.1。不能把这轮称作两版对照通过，亦不能据此推断长历史
或最终问答成绩。详细数值和限制见结果报告第 34 节；Gate 2 仍未通过。
尝试硬限落地后的最终本机全仓回归为 `599 passed`、74 条既有警告；
测试前缀四表残留均为 0，临时容器已自动移除，原有四容器仍健康。
结果报告第 35 节列出验证边界。

## 19. 两版小图媒体调用量与清理（2026-10-05）

固定合成媒体两例同源复核中，两版图文、纯图 Add 与图片 Search 均完成；
v1.0/v1.1 的带图 LLM HTTP 尝试为 3/5，usage 无缺失，整次 LLM
原价估算为 $0.00449055/$0.00782505；不是图片专属费用。
Add P95 为 3633.38/7349.58 ms（各 n=2），两版检索都非空，
v1.1 返回条数多一倍，不足以证明真实图片质量改善。精确清理后四表
与资产文件均为 0。详见报告第 37 节；独立图片质量、容量与合规
仍未验证，Gate 2 和发布门槛继续不勾选。
媒体计量改动后另用新隔离临时库跑完整回归，`601 passed`、74 条既有
警告，Ruff/mypy/差异检查通过；相关测试运行四表零残留，容器自动移除。
详见结果报告第 38 节。

## 20. 同档位 MASM 文本小样本配对（2026-10-05）

主仓库 v1.0 源码与现有工作树 v1.1 源码均以 `official-masm`、融合关闭、
相同六类自编文本样本顺序运行。v1.0/v1.1 的 Add 平均为
4875.58/3154.34 ms，P95 为 5679.08/4067.80 ms（各 n=6）；
Search P95 为 1241.01/1246.55 ms。LLM HTTP 21/16 次，Embedding
HTTP 18/13 次，失败和 usage 缺失均为 0。仅自编多事实案例的字面原子
覆盖从 0 变 1；六类标准 Recall 均为 1。两版公开原价估算合计
$0.00678015 + ¥0.000371，分币种记录，实际账单不对账。

两版各自按清单删除了六个运行前缀；隔离临时库相关四表和资产文件均无
残留，临时容器已停止并自动移除，四个原有业务容器保持健康。
详细数据与限制见结果报告第 40–41 节。本次纠正了历史 B0 比较口径，
却不是公网镜像实测；独立质量、真实图像语义、自然语言事件时间、
容量、图像向量合规和线上 SHA/digest 映射仍待验证。
**Gate 2/3 与所有发布复选框仍不勾选。**

## 21. 中止后重跑完整回归（2026-10-05）

同档位脚本改动后的完整本机 pytest 已在新建隔离 `masm_test` 重跑：
`602 passed`、74 条警告、退出码 0；Ruff、mypy（52 个源码文件）和
差异检查通过。`synthetic-holdout-%` 四表零残留；`fused-%` 测试夹具
仍有 12/12/25/0 行，不能写成全库零残留。只停止并自动移除这次的
`tmpfs` 临时数据库容器，四个原有业务容器仍健康。详见结果报告第 42 节。
这些回归不改变第 20 节的质量、容量、合规与线上映射待验证状态。

## 22. 独立来源跨案例干扰诊断（2026-10-05）

事前冻结两道 LongMemEval Oracle 正题、跨案例插入的无关会话和一题
严格空证据拒答；这是项目人工组合的小样本，不是官方基准。
两版同 `official-masm`、各 8 Add/3 Search：两道正题字面证据均满分，
严格空证据题均失败（各返回 2 条无关证据）。v1.1 未获得该集上的
独立证据覆盖增益；Add 平均 5130.06→4432.52 ms、P95
6793.36→6636.58 ms（各 n=8），不足以抵消质量证据缺口或代表容量。
两版估算合计 $0.0097278 + ¥0.0013995，实际账单不对账。
测试前缀四表及资产零残留，临时库已自动移除，四个原有业务容器健康。
详见结果报告第 43–44 节。**Gate 2/3、最终选版和发布复选框继续
不勾选；不再对这一不具辨识力的组合追加付费重跑。**

## 23. v1.0 保底材料的本机只读审计（2026-10-05）

本机 `submission-rc2` 标签解引用到 Commit
`db7d75a95c2c6a119425ed1b378023297aac03ce`；主仓库 HEAD 为
`af9173f939b652443be4f27ef438bb1d6e79e8cf`，相对标签只增加两份
设计/计划文档，`src/` 没有差异。主仓库工作区本轮只读检查无改动。
标签树内未发现 `.env`、数据库或 TSV 文件；筛出的唯一 JSON 文件是
`experiments/fixtures/small_atm.json`。这些只是路径检查，**不是内容级
密钥或隐私扫描**，更不代表线上镜像已匹配该标签。

2026-10-05 复核[赛事页](https://agentmemoryleaderboard.ai/competition/)
与[参赛说明](https://agentmemoryleaderboard.ai/rules)：开源方法榜需自行部署
稳定 Add/Search API 并提交公开仓库固定 Commit；材料截至北京时间
10 月 31 日 23:59，评测截至 11 月 4 日 23:59；Full 之前须完成
Smoke、容量与版本等检查。赛事 FAQ 指明学术榜 LLM 使用
`gpt-4o-mini`、Embedding 使用 `text-embedding-v4`。但公开页面未对
“先由 `gpt-4o-mini` 将原图结构化，再用 `text-embedding-v4` 对结构化
文字建向量”这一具体路径给出肯定答复；须向组委会取得明确确认，
**不能由本机模型名自行推断合规**。可供后续询问的精确问题是：
“开源方法榜多模态赛道中，Add/Search 接收原图并可返回原图证据，
内部先用 gpt-4o-mini 提取图像描述，再以 text-embedding-v4 对该描述
建立文本向量，是否满足图像与 Embedding 的模型约束？”本轮不发送询问。

公网验证记录仍称当时 API 镜像由 `fe540e0` 构建，并把切换到
`submission-rc2` 后重建列作待办。未获云端操作授权前，不核对当前
线上 Commit、镜像 digest、配置或备份，也不进行 DNS、推送、官方
Smoke/Full。历史小规模并发 1/2/4/8 数据不能替代本期按拟申报容量
的复验。故 v1.0 只是有历史公网 Smoke 的**保底候选**，并非已达到
本次最终提交的固定版本与合规门槛。

## 24. 标签树敏感文件与构建上下文静态审计（2026-10-05）

本机只读扫描 `submission-rc2^{commit}` 的 148 个跟踪文件：按扩展名/
文件名规则未发现 `.env`、私钥、数据库、CSV/TSV 文件；按
`sk-` 长串、AWS AKIA、GitHub `ghp_` 与 PEM 私钥头四类模式执行
`git grep -l`，命中文件数 0。现有工作树的 `src/scripts/tests/docs`
与 `.env.example` 按相同模式扫描，命中文件数也为 0。扫描只覆盖
这些规则和路径，不构成完整的密钥、样本隐私或镜像内容审计。

标签的 `pyproject.toml` 声明九项运行依赖，并把三个智能体 Prompt
以 `masm.agents` 包资源纳入 wheel；`THIRD_PARTY_NOTICES.md` 列有
相应依赖与许可证摘要。此处只核对静态配置及标签树中的三个 Prompt
路径，尚未构建 wheel、检查实际镜像内的资源或核对锁定依赖版本。

用占位值和 `.env.example` 做本机静态解析：v1.0、v1.1 的 Compose
`config -q` 均退出 0；v1.0 API 为 `127.0.0.1:8000→8000`，
v1.1 API 为 `127.0.0.1:8002→8000`，两套 Postgres 均没有宿主机
端口，API/数据库分别有资产/数据库卷；v1.1 API 连默认及边缘网络，
数据库仅连默认网络。Caddyfile 旧站点指向 `api:8000`、新站点指向
`masm-v11-api:8000`；使用本机已有 Caddy 镜像、禁网与占位域名执行
`validate` 退出 0。**静态解析不证明线上现状或实际密钥配置。**

发现发布构建风险：`submission-rc2` 的 Dockerfile 仅 `COPY`
`pyproject.toml`、`README.md`、`src`、Alembic 文件，未显示把
`.env` 或测试报告复制进镜像；但标签的 `.dockerignore` 虽排除了
`.env`，**没有排除现存的 `.worktrees/` 和 `.superpowers/`，也未列
`.env.local`**。因此不能在未经审计的脏仓库根目录直接重建保底镜像；
这些文件是否由具体 Docker/BuildKit 实现传输、或是否存在于最终镜像，
本轮未验证，也不在此声称泄漏。后续获准构建时应从固定 Commit 的
干净导出生成上下文，逐项审计包含清单并记录镜像 digest；或在新
Commit 修正忽略规则后重新冻结，不能静默移动 `submission-rc2`。
本轮没有构建、部署或改变标签。

## 25. 最终提交计划 v2 的本机执行起点（2026-10-05）

证据账本见 `masm-final-candidate-evidence-ledger-2026-10-05.md`，执行计划见
`../superpowers/plans/2026-10-05-masm-final-submission-execution-v2.md`。
本轮在现有 `codex/masm-v11` linked worktree 内核对来源、时间回归矩阵及
`MASM_FUSED_EMPTY_HISTORY_TEXT` 默认关闭状态；没有开启实验融合。

系统 Python 缺 `fastapi`，首次 pytest 在加载 `conftest.py` 时退出，未执行用例。
改用项目根 `.venv` 后，相关单元测试 **28 passed**、时间/计量数据库集成测试
**20 passed**，新建隔离 `masm_test` 上完整回归 **602 passed、74 warnings、退出码 0**。
警告仍为依赖弃用；本轮没有重跑 Ruff/mypy，也没有正式 Provider 请求。
停库前本轮 `synthetic-holdout-%` 四表为 **0/0/0/0**，`fused-%` 测试夹具
仍为 **12/12/25/0**。只停止自动删除、数据为 `tmpfs` 且仅绑定
`127.0.0.1:5433` 的本轮容器；四个原有业务容器继续健康。

再次核对[赛事官方参赛说明](https://agentmemoryleaderboard.ai/rules)：Mem-Gallery
属于本次多模态基准，因此不把它作为独立选版留出集，也不据其公开题目定向调参。
其他媒体候选仍有逐图权利或许可口径缺口；尚未冻结新样本协议，故**未运行新付费
对照，Gate 1/2/3 及发布复选框仍不勾选**。2026-10-05 的官网重查确认
[赛事页](https://agentmemoryleaderboard.ai/competition/)要求固定 Commit、自托管
Add/Search、材料及评测截止时间；具体图像描述→文本向量路径的合规性仍未得到
组委会答复。本轮没有外部联络、云端操作、生产库、容量、官网评测或推送。

## 26. 以官方现行契约核对保底版本的边界（2026-10-05）

详见 `masm-final-candidate-evidence-ledger-2026-10-05.md` 第 6 节。
本机 `submission-rc2` 固定标签的迁移头是 `0003`，当前 v1.1 是 `0005`；
两版不共用本机测试库或据此推断生产兼容性。标签树经干净导出后，
在独立 `tmpfs` 的 `masm_test` 上升级到 `0003 (head)`，其自身完整测试
为 **463 passed、74 warnings、退出码 0**；pytest 退出时另有临时目录
WinError 5 收尾警告。测试夹具非零，仅停止该一次性库容器。

首次 wheel 构建因项目 `.venv` 缺 `setuptools/wheel` 失败；换用本机已有
Anaconda 构建后退出码 0，三份 Prompt 均入包，产物哈希及限制见证据账本。
另从标签重新导出的干净上下文本机构建镜像退出码 0；只读镜像检查确认
Prompt、`0001`–`0003` 存在且 `/app/.env`、`/app/tests` 不存在。
该镜像使用当期宽范围依赖，仅是本机预检，不是线上固定 digest。
这些本机验证不证明公网 v1.0 镜像与标签一致，也不替代官方 Smoke。
[官方接入规范](https://agentmemoryleaderboard.ai/rules)要求同步 Add 200 后
立即可检索、同一 `user_id` 隔离、Search 只返回证据；本机源码静态核对
符合字段和默认媒体上限，但真实线上链路、具体图片方法合规、容量、
固定 Commit↔镜像映射仍待验证。发布复选框继续不勾选。

## 27. 独立 CC0 媒体 Gate 1 与新鲜回归（2026-10-05）

运行前已冻结 3 张 Wikimedia Commons CC0 真实照片、逐图 SHA-256、
问题、原子答案标记、同 `official-masm` 档位、18/18 HTTP 尝试上限及
最小差值/延迟门槛；详见
`masm-v11-independent-media-holdout-2026-10-05.md`。两版分别使用自身
独立临时数据库：v1.0 `0003`、v1.1 `0005`。v1.0 三题 Recall@10 为
0/3，v1.1 为 3/3；没有 case 回退、Provider 失败或 usage 缺失。
v1.1 Add P95 比 v1.0 高约 22.5%，低于事前 25% 上限；各 n=3，
不得外推容量或官方得分。两版各 3 条精确清理后四表与资产均为 0，
临时库已删除，四个原有容器健康。详细指标与结果哈希见报告第 45 节。

按事前规则，**Gate 1 判定为 `v1.1 go`**，v1.0 不再作为最终选版，
停止新增付费诊断。计量工具新增外部媒体输入的哈希、路径、格式、许可和
报告泄漏保护；随后在新的 `0005` 隔离库运行完整回归：
**607 passed、74 warnings、退出码 0**，Ruff、mypy 52 个源码文件和
差异检查均通过；`synthetic-holdout-%` 四表零残留。已知 Windows
pytest 临时链接 WinError 5 与依赖弃用警告仍如实保留。

当前只通过本机 Gate 1 和代码回归，**尚不能正式提交**：工作树未提交
改动尚未形成不可变 Commit；本机预检镜像不是从最终 Commit 构建的线上
digest；云端部署/回滚、
容量和官网 Smoke/Full 未经授权执行。Gate 2–5 与发布复选框继续不勾选。

## 28. 官网模型与内部架构规则的最终映射（2026-10-05）

本节以当天直接读取的[官方参赛说明及 API 指南](https://agentmemoryleaderboard.ai/rules)
和[赛事 FAQ](https://agentmemoryleaderboard.ai/competition/)覆盖第 23、25、26 节中
“必须另获图片向量架构确认”的旧谨慎结论。官网现行文字明确：

- 平台不规定数据库、索引或内部架构，只要求 Add/Search 契约及可审计证据；
- 多模态“原图优先，caption 兼容”，平台发送原图和 caption，支持图片的系统处理原图；
- 学术榜 Embedding 必须使用 `text-embedding-v4`，所有 LLM 相关组件必须使用
  `gpt-4o-mini`，Reranker 不限。

当前 `Settings.validate_runtime()` 对正式档位强制上述两个模型名、1024 维和 HTTPS；
`build_runtime()` 的感知、时序、治理与查询分析共用同一个 `gpt-4o-mini`
Provider，所有文本和图片描述向量均由同一个 `text-embedding-v4` Provider 产生，
Reranker 为本地词法实现。`GroundedMultimodalEmbeddingProvider` 把原始图片作为
内联 Data URI 交给感知模型，只把结构化可观察描述交给 Embedding；它没有调用
其他视觉或向量模型。真实 Provider 报告也核对到两个规定型号和融合关闭。

据此，**模型与内部架构的公开规则映射已通过**；官网没有要求先就每种内部架构
单独取得邮件批准，因此不再把外部联络作为本地 Gate 2 的必要条件。该判断不是
主办方预审或获奖保证，最终有效提交仍须通过官网的版本、结果与合规复核。
Gate 2 目前剩余阻塞是把未提交工作树形成固定 Commit，并证明该 Commit、正式
镜像 digest、公开仓库和实际部署一一对应；这些尚未完成。

## 29. 冻结前独立审查与安全修复（2026-10-05）

独立审查无 Critical；3 个 Important 已按失败测试修复：本机测试数据库 URL
拒绝可改写 libpq 路由的 query 参数，Provider 尝试上限即使被 Add/Search
降级捕获也会在评估边界终止，外部媒体拒绝符号链接并核对真实路径仍位于清单
目录内。覆盖上限的 Add/Search 两条运行边界、`host`/`hostaddr`/`service`
三种 URL 和符号链接逃逸。

修复后计量单元文件 **68 passed**；新的随机 localhost 端口、`tmpfs`、
pgvector/pg16 临时库迁移到 `0005` 后，全量 **613 passed、74 warnings、
退出码 0**，Ruff、mypy 52 个源码文件和差异检查均退出 0。全量测试库包含
一般夹具，删除前四表总数为 `291/291/600/50`，故不宣称全库零；确认迁移版本
后只删除本轮命名临时容器，四个原有容器仍健康。已知 pytest 退出后的
`pytest-current` WinError 5 与依赖弃用警告继续如实保留。

两个非阻塞 Minor 暂缓：价格卡 `source` 可接受纯空白、旧文档较早段落仍保留
后文已覆盖的谨慎表述。固定 Commit、干净导出构建、最终镜像 digest 和容器契约
复验仍是 Gate 3 的下一步；本节不授权推送、云端、生产库、容量或官方评测。

## 30. v1.1 精确证据选择候选的发布门槛（2026-10-09）

本节针对 `docs/superpowers/specs/2026-10-08-masm-v11-evidence-selection-design.md`，
不追认上文的历史候选为当前线上版本。当前已观察到的上一轮公开 Smoke 为总分
23.08，检索 66.67、原子检索 100、直接召回 0、跨会话推理 0、弃答 11.11。
新候选的目标是公开 v1.1 Smoke ≥50，**本机回归不能证明这个目标已达到**。
本机候选回归为 **725 passed、1 skipped、92 warnings**；Ruff、mypy（58 个源码文件）、
Compose 占位配置解析及差异检查均退出 0。跳过项仅在 POSIX 上验证权限语义，
警告主要来自现有依赖弃用；这些结果不等于真实 Provider、容量或公网验证。

发布前必须从推送到公开仓库的固定 Commit 克隆到新的固定来源目录，核对
`rev-parse HEAD` 与干净工作树，记录完整 Commit、镜像 ID、旧 API 容器 ID、
数据库容器 ID、旧镜像与旧 Compose 文件路径。新镜像必须使用不可变标签；不要
覆盖旧镜像标签或原位修改旧来源目录。`docker compose config --images` 可检查
镜像选择；不要把包含密钥的完整 Compose 配置输出到聊天或公开日志。新候选
无数据库迁移，只替换 API 容器，不重建 PostgreSQL、资产卷或 Caddy。

服务器终端先执行下面的只读预检；把 `<完整候选Commit>` 替换成最终推送的
40 位 SHA。若线上镜像/配置与预期不符，停止，不按截图猜测路径：

```bash
CANDIDATE_SHA='<完整候选Commit>'
SHORT="$(printf '%s' "$CANDIDATE_SHA" | cut -c1-7)"
OLD_API="$(docker inspect -f '{{.Id}}' masm-v11-api-1)"
OLD_DB="$(docker inspect -f '{{.Id}}' masm-v11-postgres-1)"
docker inspect masm-v11-api-1 --format 'old_image={{.Config.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} config_files={{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect masm-v11-postgres-1 --format 'db_image={{.Config.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
```

已知上一候选是 `masm-v11-final-candidate:cd9df2c`；以下步骤仅在预检确认
它仍是当前 API 镜像，且旧覆盖文件确实为
`/opt/masm-v11/config/image.override.cd9df2c.yml` 时适用。`git clone` 与
构建成功后，先核对 `SOURCE_OK`、`IMAGE_OK`、新旧覆盖文件仅镜像标签不同，
再由操作者执行重建 API 的命令：

```bash
R="/opt/recovery-$SHORT"
ENV=/opt/masm-v11/config/v11.env
OLD_OVERRIDE=/opt/masm-v11/config/image.override.cd9df2c.yml
NEW_OVERRIDE="/opt/masm-v11/config/image.override.$SHORT.yml"
test ! -e "$R" && git clone -b codex/masm-v11-evidence-selector --single-branch https://github.com/fhfddx/AgentMemoryChallenge "$R"
test "$(git -C "$R" rev-parse HEAD)" = "$CANDIDATE_SHA" && test -z "$(git -C "$R" status --porcelain)" && echo SOURCE_OK
docker build --pull=false -t "masm-v11-final-candidate:$SHORT" "$R"
docker image inspect "masm-v11-final-candidate:$SHORT" --format 'IMAGE_OK id={{.Id}} size={{.Size}}'
test -f "$OLD_OVERRIDE" && grep -q 'masm-v11-final-candidate:cd9df2c' "$OLD_OVERRIDE" && sed "s/masm-v11-final-candidate:cd9df2c/masm-v11-final-candidate:$SHORT/" "$OLD_OVERRIDE" > "$NEW_OVERRIDE"
diff -u "$OLD_OVERRIDE" "$NEW_OVERRIDE"
docker compose --project-name masm-v11 --env-file "$ENV" -f "$R/deployments/docker-compose.v11.yml" -f "$NEW_OVERRIDE" config --images
```

上面的 `diff` 因预期的镜像标签变化会返回 1；须人工确认没有第二处差异，
再执行：

```bash
docker compose --project-name masm-v11 --env-file "$ENV" -f "$R/deployments/docker-compose.v11.yml" -f "$NEW_OVERRIDE" up -d --no-deps api
test "$OLD_API" != "$(docker inspect -f '{{.Id}}' masm-v11-api-1)" && echo API_REPLACED
test "$OLD_DB" = "$(docker inspect -f '{{.Id}}' masm-v11-postgres-1)" && echo DB_UNCHANGED
docker inspect masm-v11-api-1 --format 'new_image={{.Config.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
docker exec masm-v11-api-1 python -c 'from masm.providers.openai_embeddings import MAX_BATCH_SIZE; print("BATCH_LIMIT_OK", MAX_BATCH_SIZE)'
curl -fsS https://v11.agentmemorydev.icu/health
```

如模块检查中的导入路径与候选源码不同，改用镜像内对应模块做只读检查；
不得把该检查的失败忽略为部署成功。还须检查内部 `GET /health`、最近日志
没有新增 5xx/密钥/请求正文泄漏，并用唯一前缀的合成用户做一次正例、一次
无关问题弃答验证。Provider 故障回退已由本机自动化测试覆盖；不要为了验证
回退而修改生产密钥或中断生产 Provider。合成探针使用既有
`scripts/delete_evaluation_run.py` 按精确 `user_id`/`request_id` 清理，随后
以只读 SQL 和资产目录检查零残留；清理前后均不得打印样本正文与密钥。

仅当镜像 ID、API 健康、数据库 ID 不变、探针清理为零全部通过后，在官网运行
**一次** v1.1 Smoke；只对照公开聚合分数，不读取隐藏题目、答案或私有运行数据。
若未达 50，保留旧镜像和配置以便回滚，先基于公开聚合结果定位，不反复定向调参。
回滚时使用预检记录的旧 Compose 文件与 `$OLD_OVERRIDE`，同样只运行
`up -d --no-deps api`，并再次核对 `$OLD_DB` 不变及内部/外部健康。
