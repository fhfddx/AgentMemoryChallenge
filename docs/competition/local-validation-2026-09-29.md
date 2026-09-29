# 本地候选版本验收记录（2026-09-29）

## 验收对象

- Git commit：`ead321c1c2d1b73a4049d1d7e3c2e50b7a8a8ccc`
- Docker 镜像：`agentmemorychallenge-api@sha256:4d0f0af3786136124a4758b6005bd716b9324f87f691e649c6eb5fe4a9f2fedb`
- 运行档位：`local-fake`
- 环境：Windows + Docker Desktop，本地 PostgreSQL/pgvector 与文件对象卷

本记录只证明无付费模型的本地候选链路，不作为真实 Provider 或比赛托管环境的验收证据。

## 自动化验证

- `pytest -q`：461 passed。
- `ruff check .`：通过。
- `mypy src`：通过（48 个源文件）。
- `docker compose config --quiet`：通过。
- 全新 PostgreSQL 命名卷启动时执行 `alembic upgrade head`，API 与数据库健康检查均通过。

## 公共 HTTP Smoke

- `GET /health`：返回 `{"status":"ok"}`。
- 文本 Add → Search：通过。
- 相同 `request_id` 重复 Add：返回相同结果。
- API 容器重启后 Search：仍能找到重启前写入的记忆。
- 真实 PNG Data URL Add → 图片 Search：返回 1 条记忆。
- 图片请求重复 Add：返回相同结果。

## 日志抽检

本次容器日志未出现：

- 本地 Smoke API Key；
- Smoke 固定标记或用户标识；
- `data:image` Base64 内容；
- Prompt/请求载荷。

## 尚未验证

- `official-baseline` 与 `official-masm` 的真实 Provider Smoke；
- `gpt-4o-mini`、`text-embedding-v4` 的供应商快照、费用与延迟；
- 文本查询召回真实图片描述/OCR 的效果；
- HTTPS 域名、证书与公网部署；
- 组委会对视觉文本化方案和模型使用规则的书面确认；
- 最终镜像摘要、Git 标签和线上配置三者一致性。
