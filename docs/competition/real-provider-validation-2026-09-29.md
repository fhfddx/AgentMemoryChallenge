# 真实模型 Provider 验证记录（2026-09-29）

## 验证范围

- 运行档位：`official-baseline`
- LLM：OpenAI `gpt-4o-mini`
- Embedding：阿里云百炼 `text-embedding-v4`，1024 维，北京地域
- 容器：独立 Compose 项目 `masm-official-smoke`，API 端口 8001
- 镜像：`sha256:76d064d132511832e4241c9ad80954f04b401cf062a673ca6f6f24264ea518c1`
- 代码：本记录所在提交

本次验证使用独立 PostgreSQL 与对象存储卷，不修改 8000 端口的 `local-fake` 环境。记录中不保存 API Key、完整工作空间域名、请求正文、图片 Base64 或模型响应正文。

## 配置问题与修复

1. 百炼工作空间域名最初保留了文档占位符的尖括号，直连探测触发 `RemoteProtocolError`。删除 URL 中的 `<`、`>` 后，Embedding 接口返回 HTTP 200，向量维度为 1024。
2. LLM 请求最初没有启用严格 JSON Schema，模型返回 Schema 之外的顶层字段，导致本地校验失败。
3. 首次加入 `strict: true` 后，OpenAI 因 Pydantic 默认字段未全部列入 `required` 而拒绝 Schema。Provider 适配层现会递归把对象属性列入 `required`、设置 `additionalProperties: false`，并移除 `default` 注解。该行为由两项回归测试覆盖。

严格 Schema 的处理依据 OpenAI Structured Outputs 官方约束：所有字段必须为 `required`，所有对象必须设置 `additionalProperties: false`。

## 真实 HTTP Smoke

- `GET /health`：HTTP 200，返回 `{"status":"ok"}`。
- 文本 Add → Search：通过。
- 相同 `request_id` 重复 Add：返回相同结果。
- 真实 PNG Data URL Add：HTTP 200。
- 使用文本 `a solid purple square` 检索图片记忆：HTTP 200，返回 1 条结果，首条包含分数。
- 最终图像 Add + Search 总耗时：4.392 秒。

## 自动化验证

- `pytest -q`：463 passed，退出码 0。
- `ruff check .`：通过。
- `mypy src`：通过，检查 48 个源文件。
- `docker compose config --quiet`：通过。
- 全新 `masm_test` PostgreSQL 执行 Alembic 0001 → 0003：通过。
- 当前正式基线容器健康检查：`healthy`。

Pytest 输出包含上游 `pytest-asyncio`、Starlette 的弃用警告，以及 Windows 临时目录清理的 `PermissionError`；测试进程退出码仍为 0，463 项全部通过。后续升级测试依赖时应消除这些警告。

## 日志抽检

对最终正式基线容器日志扫描后，以下内容均未出现：

- OpenAI API Key；
- 阿里云百炼 API Key；
- 本地 API 鉴权 Key；
- `data:image` Base64；
- Bearer 请求头；
- Add/Search 请求正文；
- 图像检索文本；
- Traceback。

## 尚未验证

- `official-masm` 全智能体档位的真实 Provider Smoke（已于 2026-09-30 完成，见
  [公网正式部署验证记录](public-deployment-validation-2026-09-30.md)）；
- 供应商未公开的精确后端模型快照；
- 公网 HTTPS 域名和证书（已于 2026-09-30 完成，见上述记录）；比赛平台正式评测回调仍待提交；
- 组委会对视觉文本化方案的书面确认；
- B0、B1、MASM 与消融实验的正式跑数、成本和指标。
