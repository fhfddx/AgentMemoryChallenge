# MASM v1.1 本机留出集与模型用量对照

状态（2026-10-03）：计量脚本、数据库集成测试、主模板与替代模板的三标记种子运行均
已完成，结果见 `masm-v11-metered-holdout-report.md`。本文件保留为复现手册，不是成绩。
线上 v1.0、官网评测任务和生产数据库不在本次操作范围内。

**档位口径更正（2026-10-05）：**本脚本历史默认 `--version v1.0` 装配
`official-baseline`（B0），不是公网记录的 `official-masm` 线上 v1.0；
默认 `v1.1` 才装配 `official-masm`。旧报告里的两版差值只能解释为
“B0 源码/档位与 v1.1 MASM”，不能直接解释为线上 v1.0 与 v1.1。
若要本机比较 v1.0 MASM 源码与 v1.1 MASM，必须在 v1.0 命令显式加
`--runtime-profile official-masm`，并在报告核对 `runtime.profile`；
旧运行与报告不追溯改写。线上镜像与 `submission-rc2` 的最终对应关系
仍未验证，不把本机源码比较冒充线上同构对照。

## 样本与记录

`scripts/metered_holdout.py` 提供 `primary`（默认）与 `alternate-v1` 两套固定模板；每套
均含偏好改写、条件计划、跨会话、多事实、干扰消息、无证据拒答 6 类。每版每套最多
6 次 Add、6 次 Search，顺序执行；两版必须使用同一个 `--run-tag` 和 `--case-set`。
查询不直接包含预期答案标记，不能在看见结果后修改样本。替代模板由本项目在主模板
运行后设计，用于语义与措辞变化下的复核，不是真实用户盲测。
两套样本均为文本输入，用来核对文本路径的调用量与费用；报告中的
`perception_calls` 统计所有 `PerceptionResult` 结构化调用，也包含纯文本 Add，不能
解释为图片专属调用数。这些结果不能推断图文或纯图片成本；此前能力对照另见
`masm-v11-formal-model-pilot.md`，但该轮没有 Provider 用量记录。

脚本通过应用内部的真实 HTTP 路由运行，只为评估期间的 LLM 与 Embedding Provider
注入计量 Client。记录每次实际 HTTP 尝试的状态、响应 `usage` 数值、Embedding
输入条数，并另记逻辑 Agent/图片感知调用次数；不保存请求正文、图片、响应全文、
认证头或 API Key。缺失 token 用量或价格配置时费用显示 `unavailable`。失败请求
也计入调用数，但不能据此推断它实际是否被计费。

后续新报告增加 `diagnostics`：`stage_latency_ms` 按固定阶段汇总次数、平均/最大
毫秒和失败数，阶段为 `perception`、`recall`、`temporal`、`curator`、
`embedding_prepare`、`commit`、`search`；`attempt_failures` 按 Provider、阶段、
白名单错误类别、HTTP 状态码和次数聚合。错误类别为 `http_status`、`timeout`、
`transport`、`response_validation`；不记录异常消息、参数或单次请求标识。
这些计量只在脚本的本机评估上下文中临时包装现有方法，退出后还原，不改变公共 API。
v1.0 没有 Add 智能体阶段，但仍记录 `search` 阶段；其余阶段仅适用于 v1.1。
阶段 `failures` 只统计**从被包装方法外逸的异常**，不是最终 API 失败数：
Provider 重试恢复或查询分析降级后，阶段可记为成功，同时
`attempt_failures` 仍记录失败尝试。HTTP 尝试失败数指非 2xx 状态或传输错误；
HTTP 200 但响应无法解析只出现在 `attempt_failures` 的
`response_validation` 类别，两项总数不要求相等。
阶段耗时有嵌套关系（例如 `recall` 包含其 Embedding 调用），不能把各阶段值
相加当作 Add 总耗时；未列出的媒体处理、锁和发布等开销也不能由此精确分摊。
若在本机显式设置 `MASM_FUSED_EMPTY_HISTORY_TEXT=1`，无历史纯文本 Add 可进入
试验性 `fused_text` 阶段；计量同时记录 `fused_text_calls`。融合输出校验失败时
可能回退至感知、时序，三个阶段可出现在同一轮报告中。默认值为 `0`，本机
三例观测不足以支持正式启用；后续六类样本还暴露了标识符数字被误判为
事件日期及频繁回退，详见结果报告第 24、26 节。当前提示词版本为
`fused-text-v2`；当前仅让恰有一处规范独立 ISO/斜杠日期，且其它位置
没有数字的输入进入融合，其余直接走旧路径。`max_history=0` 时依原设计
没有参与本次分析的历史候选，即使库中已有旧记忆也可尝试融合。
详见结果报告第 27、28 节。
当前入口的真实 Provider 时延尚未复测，不能按旧三例数字宣称质量达标。
数字入口仍可能漏掉文字日期或相对日期形成的第二个时间表达；结果报告
第 29 节将其列为未解决风险。请勿在正式环境开启此实验开关。
v1.1 在本次优化后若没有召回同用户历史候选，会确定性跳过 Curator 模型调用；
此时 `curator` 阶段不产生计时记录，不能把缺失的阶段误读为计量失效。
若有历史候选，仍执行 Curator 与动作校验。状态机在两条路径上仍经过 `CURATED`。
旧报告没有这些字段，不能追溯补算；没有新增运行时不产生新的实测瓶颈结论。

每次 Add 前会将精确的用户和运行 ID 写入独立清理清单，结束或抛错时逐一调用运行级
删除。若清理失败，保留清单并报错，必须先核对残留，不能把本次报告当成有效对照。
程序只接受主机为 `localhost`/`127.0.0.1`/`::1` 且数据库名为 `masm_test` 的
`MASM_TEST_DATABASE_URL`；资产目录必须在源码树下的 `.superpowers` 内。

## 价格口径

本次核对到的公开**原价**：OpenAI `gpt-4o-mini` 文本输入 $0.15、输出 $0.60 / 百万
tokens；阿里云百炼华北 2 `text-embedding-v4` 文本输入 ¥0.5 / 百万 tokens。
来源：[OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-4o-mini)、
[阿里云百炼模型页](https://help.aliyun.com/zh/model-studio/text-embedding-v4)。
另一个产品“PAI Token Service”的同名向量模型公开价格是 ¥0.6 / 百万 tokens，
见[其独立计费页](https://help.aliyun.com/en/pai/product-overview/billing-of-pai-token-service)。
当前 Embedding URL 路径为百炼的 `compatible-mode/v1`，因此手册示例采用百炼原价；
这仍不代表账号实际优惠或账单。脚本把美元 LLM 与人民币 Embedding **分开**展示，
不换算、不相加；OpenAI 缓存折扣也未计入。实际费用需事后用两家账单核对。

## 复现命令

在独立工作树的 PowerShell 中执行。以下读取本机已有 `.env`，只把限定的 Provider
字段放进当前进程，不输出密钥、不复制 `.env`。不要把终端环境或输出文件提交到 Git。

```powershell
$mainRoot = 'E:\Competitions\AgentMemoryChallenge'
$branchRoot = 'E:\Competitions\AgentMemoryChallenge\.worktrees\masm-v11'
$runTag = [guid]::NewGuid().ToString('N').Substring(0, 12)
$providerNames = @(
  'MASM_LLM_BASE_URL', 'MASM_LLM_API_KEY', 'MASM_LLM_MODEL',
  'MASM_EMBEDDING_BASE_URL', 'MASM_EMBEDDING_API_KEY',
  'MASM_EMBEDDING_MODEL', 'MASM_EMBEDDING_DIMENSIONS',
  'MASM_MODEL_TIMEOUT_SECONDS', 'MASM_MODEL_MAX_ATTEMPTS'
)
Get-Content -LiteralPath (Join-Path $mainRoot '.env') | ForEach-Object {
  if ($_ -match '^\s*([A-Z][A-Z0-9_]*)=(.*)$' -and $providerNames -contains $Matches[1]) {
    [Environment]::SetEnvironmentVariable(
      $Matches[1], $Matches[2].Trim().Trim('"').Trim("'"), 'Process'
    )
  }
}
$env:MASM_TEST_DATABASE_URL = 'postgresql+psycopg://postgres@127.0.0.1:5433/masm_test'
Set-Location -LiteralPath $branchRoot
$env:PYTHONPATH = Join-Path $mainRoot 'src'
$v10Args = @(
  '--source-root', $mainRoot, '--version', 'v1.0', '--case-set', 'primary',
  '--run-tag', $runTag,
  '--asset-dir', (Join-Path $branchRoot ".superpowers\metered-holdout\v10-$runTag"),
  '--output', (Join-Path $branchRoot ".superpowers\metered-holdout\v10-$runTag.json"),
  '--cleanup-output', (Join-Path $branchRoot ".superpowers\metered-holdout\v10-$runTag.tsv"),
  '--price-source', 'OpenAI model page + Aliyun Model Studio Beijing model page',
  '--llm-input-rate-usd', '0.15', '--llm-output-rate-usd', '0.60',
  '--embedding-input-rate-cny', '0.5'
)
& (Join-Path $mainRoot '.venv\Scripts\python.exe') -m scripts.metered_holdout @v10Args
if ($LASTEXITCODE -ne 0) { throw 'v1.0 留出集运行失败，停止对照' }
$env:PYTHONPATH = Join-Path $branchRoot 'src'
$v11Args = @(
  '--source-root', $branchRoot, '--version', 'v1.1', '--case-set', 'primary',
  '--run-tag', $runTag,
  '--asset-dir', (Join-Path $branchRoot ".superpowers\metered-holdout\v11-$runTag"),
  '--output', (Join-Path $branchRoot ".superpowers\metered-holdout\v11-$runTag.json"),
  '--cleanup-output', (Join-Path $branchRoot ".superpowers\metered-holdout\v11-$runTag.tsv"),
  '--price-source', 'OpenAI model page + Aliyun Model Studio Beijing model page',
  '--llm-input-rate-usd', '0.15', '--llm-output-rate-usd', '0.60',
  '--embedding-input-rate-cny', '0.5'
)
& (Join-Path $mainRoot '.venv\Scripts\python.exe') -m scripts.metered_holdout @v11Args
if ($LASTEXITCODE -ne 0) { throw 'v1.1 留出集运行失败' }
```

复现前先运行数据库集成测试；第一版若失败，不运行第二版。要复现替代模板，将两处
`'primary'` 同时改为 `'alternate-v1'`。比较两份 JSON
的类别召回、原子覆盖、P95、响应字节、LLM/Embedding 调用与 token 用量，并记录两家
账单与原价估算的差别。在结果和费用经过审阅前，继续暂缓 16/16 容量与官方 Smoke。

## 独立来源文本小样本模式（已运行一次）

脚本另支持 `--case-set external --external-cases <JSON 路径>
--external-cases-sha256 <预先记录的 SHA-256>`。两版必须使用同一份文件和同一个
哈希；文件发生变化即在创建应用、连接测试库或请求 Provider 之前失败。不要把此
模式称为官方评测或隐藏盲测。公开数据集的子集只能称为“独立来源小样本”。

外部 JSON 顶层恰有 `source_name`、无查询参数或片段的公开 HTTPS
`source_url`、事前确定的 `selection_rule`、`cases`。正例 case 恰有 `id`、唯一
`category`、`sessions`（会话数组，每条消息仅 `role` 与纯文本 `content`）、
`query`、`expected_markers`。答案标记必须逐字出现在来源消息中、不得出现在查询中；
此口径衡量检索证据的字面覆盖，不等于最终问答正确率。文件最多 128 KiB、6 个
case、8 次 Add、24 条消息和 12,000 字正文；不接收图片。报告只保存来源名称、
公开 URL、抽样规则、源 item ID、文件 SHA-256 与聚合指标，不保存消息、查询或
答案正文。清理方式与内置模板相同。
拒答 case 可额外写唯一可选字段 `"expect_empty": true`，此时
`"expected_markers": []`，但仍须有 1–3 个来源会话；Search 返回空列表才得分 1。
缺少此显式字段的空标记、以及标记非空却要求拒答的案例均在发起请求前拒绝。
这是严格“空证据”口径，不等于最终问答模型能否根据无关证据拒答，也不能用
没有任何历史的空用户案例代替有干扰历史的测试。
校验还会拒绝空白答案标记、答案标记出现在报告元数据或类别/ID 中、非公开本机地址，
以及无法编码为 UTF-8 的字段；这只能防止已声明标记的直接泄漏，不能代替对来源
元数据和样本内容的人工审阅。

实际使用的 3 案例样本来自 Apache-2.0 的 Memora 公开 weekly 数据，固定提交、
抽样规则、输入哈希和两版结果见结果报告第 12 节。样本文件保留在本地
`.superpowers/metered-holdout/external-memora-weekly-s1.json`，不得按运行结果改选；
它是公开来源的极小摘录，不是盲测或官方评测。新增来源仍须先核实许可、固定规则和
文件哈希，再运行两版。

## 图片用量的限定计量边界（2026-10-05）

本机计量器现可对每次实际 LLM HTTP 尝试，仅按 `messages[].content[]` 中
`type=image_url` 的结构计数，报告 `llm_image_attempts`、
`llm_image_blocks`、`image_usage_missing`；失败/重试也分别计入。
它不保留图片 URL、Data URI、字节或正文，文本 Embedding 请求不算图片请求。
这只是计量底座的 Fake HTTP 单元验证：**还没有冻结独立媒体样本，也没有通过
带图片的真实 Provider 运行或媒体专属清理集成测试**。LLM 返回的 `usage`
同时涵盖同次请求的文字与图片，无法从这些字段拆出图片专属 token 或金额；
图片费用仍为 `unavailable`，不能把整次 LLM 原价估算称作图片费用。

另已冻结 LongMemEval Oracle 三类文本证据摘录，固定来源修订与本地文件
SHA-256 见结果报告第 32 节。它只含相关发言；第 33 节另行固定了
LLM/Embedding HTTP 尝试硬上限和运行后费用停止线。脚本可通过
`--max-llm-attempts`、`--max-embedding-attempts` 设置每版尝试上限，
含失败重试；达到上限即在下次发送前失败并进入精确清理。该硬上限
不能保证货币账单上限，缺失 usage 时实际费用仍不可知。第 34 节只完成
v1.0 基线，因其在摘录的证据量尺已满分，未对该样本运行 v1.1；
不能称为两版对照，更不能替代完整 Gate 2。
另有内置 `media-v1`：两张项目自编 8×8 PNG 的图文/纯图路径诊断，
每版 2 Add、2 Search；两版结果和清理见报告第 37 节。它能计数
图片相关 LLM HTTP 请求，不是独立媒体质量集，图片专属费用仍为
`unavailable`。不要把它的非空检索率或整次 LLM 费用写作真实图片成绩。
