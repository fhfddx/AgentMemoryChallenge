# MASM v1.1 —— Full 评测提交说明（草稿）

对应 FULL EVALUATION GATE 清单第 4、5、6 项。复制到表单对应字段即可；方括号处填当次实际值。

---

## 5. Deployment information（部署信息）

| 项 | 值 |
| --- | --- |
| 赛道 | 开源方法（Open-source Methods）· 多模态 · Cycle 2 |
| 提交版本 | `masm-v11-final-candidate:939c77f`（git commit `939c77f`，源码摘要 `e7839485…`） |
| 端点 | `POST /add`、`POST /search`、`GET /health`（严格遵循官方 Add/Search 契约） |
| 鉴权 | 请求头 `X-Api-Key: <KEY>`；密钥由评测账号持有，不出现在任何日志中 |
| 基址 | `https://[DOMAIN]`（公开）与容器内 `http://127.0.0.1:8000`（自检） |
| 运行档位 | `MASM_RUNTIME_PROFILE=official-masm`（三智能体 Add 治理 + 证据图选择器 + 拒答预检） |
| 存储 | PostgreSQL 16 + pgvector，独立卷 `masm-v11-postgres-data`；当前 [N] 条记忆 |
| 主机 | 单实例 ECS，2 vCPU / 3.5 GiB；磁盘余量 ≥ 13 GiB |
| 并发 | `MASM_MAX_CONCURRENT_REQUESTS=16`，`MASM_MAX_REQUESTS_PER_MINUTE=1000` |
| 限额 | `top_k ≤ 100`，单次搜索响应 ≤ 30 MiB，单条图片 ≤ 10 MiB |
| 模型调用 | LLM `gpt-4o-mini`，向量 `text-embedding-v4`（1024 维），均通过 OpenAI 兼容接口；超时 30 s、最多 2 次尝试 |

### 运维约束（评测方需要知道的行为）

- **Add 幂等**：同一 `request_id` 重复提交不会写入重复记忆；中断后可安全重试。
- **Provider 故障的语义**：上游网络错误、超时、非 2xx 或非法向量响应统一返回 **HTTP 503**，响应体不含供应商正文。收到 503 表示"本次未写入"，重试即可。
- **向量批量上限**：embedding 供应商单次同步请求最多 10 条文本，代码已按批拆分；并发过高时可能出现 429，服务会退避重试（最多 2 次），仍失败则返回 503。
- **不写生成式答案**：`/search` 只返回证据条目（`id` / `content` / `score`），不生成自然语言答案。
- **删除接口**：仅用于自检数据清理，不参与评测流程。

---

## 4. Full evaluation capacity（容量确认）

| 维度 | 规划 | 余量 |
| --- | --- | --- |
| 存储 | 每条记忆含 1024 维向量与正文，实测 130 条/用户；磁盘余 13 GiB 可承载约 [X] 万条 | 充足 |
| 带宽 | 单次搜索响应上限 30 MiB，常规响应 < 20 KiB | 充足 |
| 请求处理 | 16 并发、1000 rpm；Add 为 CPU 轻、上游延迟重，实测单条 Add 数秒 | 与填报并发一致 |
| 上游额度 | LLM 与 embedding 均已充值并预留余量；Full 期间按 60 s 间隔监控 429/503 | 已确认 |

---

## 6. Original work（原创性与复用声明）

### 本次工作为原创

- 系统架构、检索融合、证据图选择器、跨会话桥接、拒答判据与全部评测工具均为本项目自研，代码在
  `src/masm/` 与 `scripts/`。
- 本周期新增并已合并的内容：**选择器之前的拒答预检**（关系前缀主体缺失，判在被调用之前）、
  **词面通道修复**（`plainto_tsquery` 的 AND 语义导致该通道从未命中，改为实词 OR + 停用词过滤）、
  以及自检工具 `scripts/server_verify_refusal_gate.py`、`scripts/abstention_noise_probe.py`。

### 复用与依赖（按名称与许可证披露）

- 运行时依赖：FastAPI、Pydantic、SQLAlchemy、Alembic、psycopg、pgvector、Pillow、httpx、uvicorn
  （均为公开发布的开源库，许可证与来源见仓库 `THIRD_PARTY_NOTICES.md`）。
- 数据库扩展：**pgvector**（PostgreSQL 向量索引扩展）。
- 模型服务：Add 阶段使用 `gpt-4o-mini`，向量化使用 `text-embedding-v4`，均通过供应商 OpenAI 兼容
  API 调用；未使用任何第三方检索/重排模型（重排为自研词面重排）。
- 未复用他人仓库或论文代码；若评审需要具体依赖版本清单，可提供容器镜像 `pip freeze`。

### 评测诚信

- 未使用提示注入、基准泄漏或结果篡改：本周期内**从未获得、也未曾尝试获取**官方题面；所有判据
  均来自自建合成探针（`scripts/abstention_path_probe.py`、`scripts/abstention_noise_probe.py`、
  `scripts/multi_session_*_probe.py`、`scripts/evidence_graph_probe.py`）。
- 官方运行只读取公开返回的分数（四项能力分），不做任何针对具体题目的拟合。
