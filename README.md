# MASM：多智能体结构化记忆

MASM（Multi-Agent Structured Memory）是一个面向 [Agent Memory Challenge](https://agentmemoryleaderboard.ai/) Cycle 2
多模态开源方法组的长记忆系统。它通过一套 Add/Search 服务，将文本与图片内容写入结构化记忆，并在同一用户作用域内检索相关证据。

## 公共接口

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 公开浅健康检查，只返回状态，不暴露依赖机密。 |
| `POST /add` | 写入保持原始顺序的文本/图片消息，返回成功后记忆已持久化并可立即检索。 |
| `POST /search` | 在指定 `user_id` 作用域内返回相关记忆证据，只返回证据，不生成最终答案。 |

详细契约见 `docs/superpowers/specs/2026-09-28-agent-memory-challenge-design.md`。
`https://agentmemorydev.icu` 是 2026-09-30 验证过的历史 v1.0 公网服务，不能代表
当前 v1.1 候选。其当时的 Smoke、容量、备份恢复和端口加固证据见
[公网部署验证记录](docs/competition/public-deployment-validation-2026-09-30.md)。v1.1 在固定
Commit、镜像 digest 与独立部署完成前没有可申报的公网端点。

## 运行档位

| 档位 | 模型与组件 | 用途 |
| --- | --- | --- |
| `local-fake` | 确定性 Fake Embedding，不调用外部模型 | 开发、自动化测试、Docker Smoke |
| `official-baseline` | `gpt-4o-mini` 图片感知 + `text-embedding-v4` | B0 消融对照；不是公网 MASM v1.0 保底档位 |
| `official-masm` | 正式模型 + 三智能体 + 一跳关系扩展 + 重排 | 公网 MASM v1.0 的记录档位、v1.1 正式候选档位 |

默认档位是 `local-fake`，只能用于开发，禁止作为正式比赛提交。正式档位缺少模型密钥、使用
错误模型或非 HTTPS Provider 地址时会在启动阶段失败，不会静默回退到 Fake。

图片在正式档位中先由 `gpt-4o-mini` 提取可观察描述、OCR、实体和关键词，再与文本一起进入
`text-embedding-v4` 向量空间。Embedding 依赖不可用时 Add/Search 返回脱敏的 HTTP 503。

v1.1 的 `official-masm` Search 先按原问题召回，不把多选项当作独立检索词；随后最多额外调用
一次已有的 `gpt-4o-mini` Provider，只让模型从至多 32 条候选中选择至多 12 条原始证据，
证据不足时可返回空列表。候选文本会发给该 Provider：单条默认最多 1200 字符、配置上限
4096 字符；问题、选项和候选总数也有硬上限，原图字节不会发送给证据选择器。选择失败时
Search 保持 HTTP 200，只退回原问题信号准入的短证据列表，不把模型异常正文写入日志。
纯图片查询因选择器无法看到查询图像，同样使用已通过图片/语义相关性门槛的短证据列表。
这会增加一次 Search 的模型费用与延迟；默认 `local-fake` 不调用它，`official-baseline` 不启用它。
运维参数见 `.env.example` 中的 `MASM_EVIDENCE_SELECTOR_ENABLED`、
`MASM_SELECTOR_MAX_CANDIDATES`、`MASM_SELECTOR_MAX_SELECTED`、
`MASM_SELECTOR_MAX_CHARS_PER_CANDIDATE`、`MASM_SELECTOR_TIMEOUT_SECONDS` 和
`MASM_MIN_LEXICAL_RANK`。正式发布前应核对现有 Provider 的数据处理政策及预算。

## 技术栈

Python 3.11+、FastAPI、Pydantic v2、PostgreSQL + pgvector、本地持久卷对象存储、HTTPX。

## 开发环境

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev]"   # Linux/macOS

# 2. 复制环境变量示例并填入真实值
copy .env.example .env

# 3. 运行测试
.venv\Scripts\python -m pytest -v

# 4. 静态检查与类型检查
.venv\Scripts\ruff check .
.venv\Scripts\mypy src
```

测试默认连接本机 `127.0.0.1:5433/masm_test`；可通过 `MASM_TEST_DATABASE_URL` 覆盖，
但数据库名必须严格为 `masm_test`。

## 容器部署与 Smoke

```bash
# .env 中至少修改 MASM_API_KEYS 和 POSTGRES_PASSWORD；真实模型测试还要配置 Provider
docker compose up -d --build
python scripts/smoke_test.py --base-url http://localhost:8000 --api-key test-key
```

Smoke 覆盖公开 Health、Add、立即 Search 和重复 Add。复制它输出的三个标识，重启 API 后运行
`--verify-request-id`、`--verify-user-id`、`--verify-marker` 可验证持久性。HTTPS、备份、健康检查
和当前对象存储边界见 `deployments/README.md`。

## 可复现实验

`experiments/` 提供 B0、B1、完整 MASM、六组消融配置、ATM-Bench/Mem-Gallery 规范化适配器，
以及指标计算和汇总工具。运行器只调用公共 Add/Search HTTP 接口；具体命令、配置占位符和
隐私边界见 `experiments/README.md`。

## 目录结构

```text
src/masm/
  api/         HTTP 适配层（路由、认证、请求映射）
  schemas/     官方契约及智能体结构化输出模型
  config.py    应用配置
tests/
  contract/    官方 HTTP 契约测试
experiments/   可复现实验配置、公开 API 运行器与汇总工具
```

## 当前状态

已完成结构化记忆写入、混合检索、多智能体治理、隐私删除、容器候选部署、正式模型 Provider
和可复现实验框架。`official-masm` 会把三智能体与增强检索接入实际 API；默认
`local-fake` 则保留无密钥基线。历史 v1.0 公网 `official-masm` 曾完成真实文本/图片
Add/Search、用户隔离、重启持久性、小规模容量以及备份恢复验证；这些记录不自动转移给
v1.1。正式基准实验与论文消融结果尚未跑数，不能把 Smoke 结果表述为最终榜单效果。
公共线上接口仍严格限制为 `GET /health`、`POST /add`、`POST /search`。
