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
当前比赛候选部署在 `https://agentmemorydev.icu`，公网正式档位的 Smoke、容量、备份恢复和
端口加固证据见[公网部署验证记录](docs/competition/public-deployment-validation-2026-09-30.md)。

## 运行档位

| 档位 | 模型与组件 | 用途 |
| --- | --- | --- |
| `local-fake` | 确定性 Fake Embedding，不调用外部模型 | 开发、自动化测试、Docker Smoke |
| `official-baseline` | `gpt-4o-mini` 图片感知 + `text-embedding-v4` | B0、保底候选 |
| `official-masm` | 正式模型 + 三智能体 + 一跳关系扩展 + 重排 | 完整 MASM 候选 |

默认档位是 `local-fake`，只能用于开发，禁止作为正式比赛提交。正式档位缺少模型密钥、使用
错误模型或非 HTTPS Provider 地址时会在启动阶段失败，不会静默回退到 Fake。

图片在正式档位中先由 `gpt-4o-mini` 提取可观察描述、OCR、实体和关键词，再与文本一起进入
`text-embedding-v4` 向量空间。Embedding 依赖不可用时 Add/Search 返回脱敏的 HTTP 503。

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
`local-fake` 则保留无密钥基线。公网 `official-masm` 已完成真实文本/图片 Add/Search、用户隔离、
重启持久性、小规模容量以及备份恢复验证。正式基准实验与论文消融结果尚未跑数，不能把 Smoke
结果表述为最终榜单效果。公共线上接口仍严格限制为 `GET /health`、`POST /add`、`POST /search`。
