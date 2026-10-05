# MASM v1.1 最终候选文件清单（冻结前）

日期：2026-10-05。状态：`v1.1 go` 的本机固定候选；本文件所在最终 Git
提交的 SHA/tree 由 Git 与本机外部构建记录读取，避免在提交内自引用。尚未
推送、部署或运行官方评测。

## 1. 当前身份与运行时指纹

- 工作树：现有 linked worktree `codex/masm-v11`。
- 基础 HEAD：`1c7b26a8a0e924cb9343a5afdc47185b25b0841b`；该 SHA 不包含下列未提交改动。
- 运行档位：`official-masm`；`MASM_FUSED_EMPTY_HISTORY_TEXT` 默认关闭且候选不启用。
- Alembic head：`0005`。
- 运行时构建输入：`Dockerfile`、`pyproject.toml`、`README.md`、
  `alembic.ini`、`src/**`、`alembic/**`，共 121 个文件。
- 固定提交导出中的上述路径按相对路径排序，并以
  `path<TAB>file_sha256<LF>` 组成清单后的 SHA-256：
  `76f8183d86af2474b39de88aa1e50ad26f532adf0bbe67c1e0eb658f112e7363`。
  该值只用于本机内容对照，不是 Git tree hash 或容器 digest。

## 2. 拟纳入固定 Commit

除本清单自身外，当前共 42 个已修改或新增路径。

发布边界与说明：

```text
.dockerignore
.env.example
.gitignore
README.md
```

正式运行代码与默认关闭的实验实现：

```text
src/masm/agents/fused_text.py
src/masm/agents/prompts/fused_text_v2.txt
src/masm/api/app.py
src/masm/config.py
src/masm/orchestration/add_pipeline.py
src/masm/providers/llm.py
src/masm/providers/multimodal_embeddings.py
src/masm/providers/openai_embeddings.py
src/masm/runtime.py
src/masm/schemas/agents.py
src/masm/storage/repositories.py
```

测试与本机计量工具：

```text
scripts/metered_holdout.py
tests/integration/test_agent_add_pipeline.py
tests/integration/test_agent_degradation.py
tests/integration/test_fused_text_pipeline.py
tests/integration/test_metered_holdout_run.py
tests/integration/test_runtime_profiles.py
tests/unit/agents/test_fused_text_agent.py
tests/unit/agents/test_llm_provider.py
tests/unit/experiments/test_metered_holdout.py
tests/unit/providers/test_multimodal_embeddings.py
tests/unit/test_runtime_factory.py
tests/unit/test_runtime_settings.py
```

比赛说明、证据与设计记录：

```text
docs/competition/masm-final-candidate-evidence-ledger-2026-10-05.md
docs/competition/masm-v11-formal-model-pilot.md
docs/competition/masm-v11-handoff.md
docs/competition/masm-v11-independent-media-holdout-2026-10-05.md
docs/competition/masm-v11-metered-holdout-report.md
docs/competition/masm-v11-metered-holdout.md
docs/competition/masm-v11-release-checklist.md
docs/competition/masm-v11-submission-draft-2026-10-05.md
docs/competition/masm-v11-temporal-regression-matrix.md
docs/competition/submission-checklist.md
docs/superpowers/plans/2026-10-04-empty-history-write-latency.md
docs/superpowers/plans/2026-10-05-masm-final-submission-execution-v2.md
docs/superpowers/plans/2026-10-05-masm-final-submission-roadmap.md
docs/superpowers/specs/2026-10-04-empty-history-write-latency-design.md
docs/superpowers/specs/2026-10-05-masm-media-metering-diagnostic.md
```

固定前须再次以 `git status --short` 核对本文件加入后的完整集合，不得使用
无审计的 `git add .`。默认关闭的融合代码虽然拟保留用于披露实验过程，但正式
配置必须保持关闭；若审阅认为其不应进入最终公开 Commit，须在不丢失工作的前提下
另行拆分，而不能直接删除。

## 3. 明确排除

- `.superpowers/**`：本机 JSON/TSV、测试临时目录、图片、构建上下文与资产；
  已加入 `.gitignore` 和 `.dockerignore`，现有文件原样保留。
- `.worktrees/**`、`.env`、`.env.*`（仅 `.env.example` 可纳入）。
- `C:/Users/23952/AppData/Local/Temp/masm-holdout-20261005/**` 三张原图与冻结清单；
  仅保留公开 URL、许可和 SHA-256 到文档，原图不进仓库/镜像。
- 用户下载目录中的账单 CSV、截图及任何 Provider 密钥或账单原始记录。
- 本机 PostgreSQL 数据、资产卷、pytest 临时文件、wheel、临时导出和本机镜像层。
- `.superpowers/metered-holdout/**` 的脱敏报告也不进公开 Commit；文档只引用聚合指标和哈希。

固定前对全部 43 个候选路径重跑有限密钥模式扫描（OpenAI 风格 key、GitHub
token、AWS AKIA、PEM 私钥头、长 Bearer token），命中文件 0；高风险路径/
扩展名 0。人工核对 `.env.example` 只有占位凭据。这不是完整秘密扫描，暂存后
仍须核对索引集合与本清单一致。

## 4. 已验证与冻结前缺口

已验证：独立图片 Gate 1 `v1.1 go`；独立审查的 3 个 Important 已经
RED→GREEN 修复，当前完整回归 `613 passed`；Ruff、mypy、差异检查通过；
单元/集成/真实小样本清理完成；官方公开模型和内部架构规则映射通过。

本机已完成：独立代码审阅的 3 个 Important 修复；43 个路径的显式暂存与
本地固定提交；从固定提交的干净导出重建 wheel/镜像并复验资源、迁移、全量测试
和容器契约。具体 Commit/tree/wheel/image 哈希保存在不进入公开仓库的本机执行记录，
提交表单时从 Git 和镜像工具重新读取。公开推送、云端部署、容量和官网 Smoke/Full
仍分别受外部授权关口约束。
