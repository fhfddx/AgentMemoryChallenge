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

## 技术栈

Python 3.11+、FastAPI、Pydantic v2、PostgreSQL + pgvector、兼容 S3 的对象存储。

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

## 目录结构

```text
src/masm/
  api/         HTTP 适配层（路由、认证、请求映射）
  schemas/     官方契约及智能体结构化输出模型
  config.py    应用配置
tests/
  contract/    官方 HTTP 契约测试
```

## 当前状态

任务 1：项目基础、配置与官方 API Schema。
