# MASM 部署说明

## 本地候选版本

1. 复制 `.env.example` 为 `.env`，至少修改 `MASM_API_KEYS` 和 `POSTGRES_PASSWORD`。
   无模型本地验证保持 `MASM_RUNTIME_PROFILE=local-fake`。
2. 执行 `docker compose up -d --build`。
3. 执行 `python scripts/smoke_test.py --base-url http://localhost:8000 --api-key <KEY>`。
4. 执行 `docker compose restart api`，再用上一步输出的 `request_id` 运行：

   ```bash
   python scripts/smoke_test.py --base-url http://localhost:8000 --api-key <KEY> \
     --verify-request-id <REQUEST_ID> --verify-user-id <USER_ID> --verify-marker <MARKER>
   ```

Compose 默认启动 API 与 PostgreSQL + pgvector，数据库和图片目录分别写入命名卷
`postgres_data` 与 `asset_data`。启动时容器自动执行 Alembic 迁移。

## 正式模型档位

比赛候选必须把 `MASM_RUNTIME_PROFILE` 显式设置为：

- `official-baseline`：正式图文向量链路，不启用 Add 治理智能体。
- `official-masm`：正式图文向量链路、三智能体、一跳关系扩展和重排全部启用。

同时配置两套 OpenAI-compatible Provider：

```dotenv
MASM_LLM_BASE_URL=https://<LLM_PROVIDER>/v1
MASM_LLM_API_KEY=<SECRET>
MASM_LLM_MODEL=gpt-4o-mini
MASM_EMBEDDING_BASE_URL=https://<EMBEDDING_PROVIDER>/v1
MASM_EMBEDDING_API_KEY=<SECRET>
MASM_EMBEDDING_MODEL=text-embedding-v4
MASM_EMBEDDING_DIMENSIONS=1024
```

两套服务可能来自不同供应商，Base URL 和密钥必须分别配置。正式档位会校验 HTTPS、模型名、
维度、超时和重试次数；不合规时 API 不启动。当前模型组合仍须在提交前向组委会确认，尤其是
“图片经 `gpt-4o-mini` 结构化后进入 `text-embedding-v4`”是否满足多模态 Embedding 约束。

Provider 网络错误、超时、非 2xx 或非法向量响应统一映射为不含供应商响应正文的 HTTP 503。
Add 在向量生成失败时发生在 claim 前，不留下 PROCESSING 账本或图片暂存，调用方可以安全重试。

## HTTPS

设置 `MASM_DOMAIN` 指向已解析的域名，然后执行：

```bash
docker compose --profile https up -d --build
```

Caddy 自动申请和续期证书。内网或 localhost 部署使用 Caddy 本地证书时，客户端需要信任其根证书。

## 健康检查

- `GET /health` 是公开浅检查，只证明 HTTP 进程可响应。
- `application.state.dependency_probe()` 是内部依赖检查，只返回数据库和对象目录的
  `ok/unavailable`，不会返回异常、路径、连接串或密钥；它未注册为公共 HTTP 接口。

## 对象存储边界

当前候选版本实现的是文件系统 `AssetStore`，容器部署通过命名卷保证重启持久性。
外部 S3/兼容服务尚未实现适配器，不能仅靠环境变量切换；若比赛托管环境要求多副本或外部 S3，
必须先实现同等的 staging/publish/delete 事务语义并重跑并发与删除测试。单机小规模原型使用当前命名卷。

## 运维注意

- 不要把 `.env`、API Key、数据库口令或模型密钥提交到仓库。
- 备份时同时备份 `postgres_data` 和 `asset_data`，二者必须属于同一发布快照。
- 日志只允许请求标识、匿名用户标识、延迟、数量、模型版本、token 与成本元数据。
- 日志和错误响应不得包含比赛原文、Base64、Prompt 载荷、模型响应正文或 Provider 密钥。
- 正式部署先运行小型真实模型 Smoke，再运行公开基准；不得用 Mock 测试冒充真实模型证据。
