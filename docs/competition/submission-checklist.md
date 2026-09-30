# Agent Memory Challenge 提交检查表

## 代码与版本

- [x] 公开仓库可访问，README 已包含安装、部署和验证入口。
- [x] 冻结候选使用不可变标签 `submission-rc2`；旧 `submission-rc1` 保持原指向，不覆盖历史。
- [ ] 镜像摘要、线上配置和 Git commit 属于同一构建。
- [x] `THIRD_PARTY_NOTICES.md` 已记录当前依赖、许可证、代码来源和后续论文引用要求。
- [x] 已检查受 Git 跟踪文件，不包含 `.env`、真实 API Key、数据库口令、模型密钥、原始评测数据或运行产物。

## 接口与认证

- [x] `https://agentmemorydev.icu` 可访问，Let's Encrypt HTTPS 证书有效。
- [x] `GET /health` 无认证返回 HTTP 200，且只返回 `{"status":"ok"}`。
- [x] `/add`、`/search` 已使用比赛专用 `X-Api-Key` 验证；错误 Key 返回 HTTP 401。
- [x] Add/Search Schema、错误码、`options` 和 `top_k=100` 上限已在公网 Smoke 中验证。
- [x] 内部依赖探针不作为公共路由暴露，Health 不输出连接串、路径、异常或密钥。

## 模型与运行档位

配置与验收顺序见 [正式模型 Provider 配置指南](provider-setup.md)。

- [x] 已按赛事公开规则选择开源方法榜的 LLM 与 Embedding 型号。
- [x] 本地正式档位 Smoke 显式使用 `official-baseline`，没有使用 `local-fake`。
- [x] 公网正式部署显式使用 `official-masm`。
- [x] 已记录 2026-09-30 的模型标识：OpenAI `gpt-4o-mini`、阿里云百炼 `text-embedding-v4`、1024 维；供应商未公开精确后端快照。
- [ ] 图片经视觉结构化进入文本向量空间的方案已获组委会确认。
- [x] 真实图片 Add 与文本跨模态 Search Smoke 已通过。
- [x] Provider 故障返回脱敏 503；真实档位日志抽检无原文、Base64、Prompt 载荷、响应正文或密钥。

## 容量、超时和持久性

- [x] 单图 10 MiB、单 Add 图片 30 MiB、Search 响应 30 MiB 的边界已通过自动化测试验证。
- [x] 已记录 Provider 超时/重试和公网建议超时；小规模容量测试覆盖 Add/Search 并发 1、2、4、8。
- [x] PostgreSQL 和图片对象使用持久卷；一致性备份校验与隔离恢复演练均覆盖两者。
- [x] 本地 `local-fake` 与公网 `official-masm` 均通过 API 重启后的持久性验证。
- [ ] 当前单机文件对象存储限制已获接受；若改为外部 S3，重新验证 staging、发布和删除语义。

## 验证证据

- [x] `pytest -q` 全部通过（463 项）。
- [x] `ruff check .` 与 `mypy src` 全部通过。
- [x] 空数据库执行 `alembic upgrade head` 成功。
- [x] `docker compose up -d --build` 后 Health/Add/Search/重复 Add Smoke 通过（`local-fake`）。
- [x] `docker compose restart api` 后持久性 Smoke 通过（`local-fake`）。
- [x] 全路径用户隔离测试通过；本地容器日志抽检不含原文、Base64、API Key 或 Prompt。
- [x] `official-baseline` 真实 Provider 文本与图片 Smoke 通过，见 [真实模型验证记录](real-provider-validation-2026-09-29.md)。
- [x] 公网 `official-masm` 多模态 Smoke、容量、备份恢复和端口加固通过，见
  [公网正式部署验证记录](public-deployment-validation-2026-09-30.md)。
- [ ] B0、B1、MASM 和消融实验清单包含 commit、模型、Prompt、数据集、seed 与成本。
