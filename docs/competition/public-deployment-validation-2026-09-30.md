# 公网正式部署验证记录（2026-09-30）

## 验证对象

- 比赛方向：Agent Memory Challenge Cycle 2，Multimodal Memory / Open-source Methods
- 公网域名：`https://agentmemorydev.icu`
- 云环境：华为云新加坡区域，Ubuntu 24.04，2 vCPU / 4 GiB
- 运行档位：`official-masm`
- LLM：OpenAI `gpt-4o-mini`
- Embedding：阿里云百炼 `text-embedding-v4`，1024 维
- 数据层：PostgreSQL 16 + pgvector、Docker 命名卷文件对象存储

本记录不包含 Memory System Key、模型 Provider Key、数据库口令、请求正文、图片 Base64、
模型 Prompt 或响应正文。

## 版本关系

本轮公网验证时，API 镜像由实现提交 `fe540e0` 构建，镜像 ID 为
`sha256:7452dbc68f1f1c20b5b51bb0ea7c0949710b6e4e5f08ce543499073b1ff0b006`；随后从
`93faa94` 应用了 API 回环绑定和 Docker 日志轮转配置。旧标签 `submission-rc1` 已指向更早的
`92afc26`，因此不移动旧标签，新的冻结候选使用 `submission-rc2`。

`submission-rc2` 建立后还需在服务器上完整切换到该标签并重建镜像，届时再完成“线上镜像、
配置和 Git 标签属于同一构建”的最终检查。本记录不会把尚未执行的对齐步骤标记为完成。

## 公网接口与 HTTPS

| 项目 | 验证结果 |
| --- | --- |
| `GET https://agentmemorydev.icu/health` | HTTP 200，正文为 `{"status":"ok"}` |
| `POST /add` | 比赛专用 `X-Api-Key` 调用成功 |
| `POST /search` | 比赛专用 `X-Api-Key` 调用成功 |
| 错误 Key | HTTP 401 |
| 未认证 Health | 可公开访问，不暴露内部依赖信息 |
| API 宿主机端口 | `127.0.0.1:8001`，不直接暴露公网 |
| 公网监听 | TCP 80、443；Caddy 反向代理到 Compose 内部 `api:8000` |

TLS 证书验证结果：

- Subject：`CN = agentmemorydev.icu`
- Issuer：Let's Encrypt `YE2`
- 有效期：2026-09-30 01:24:26 UTC 至 2026-12-29 01:24:25 UTC

## 正式多模态 Smoke

在 `official-masm` 档位完成以下公网验证：

1. 文本 Add 后立即 Search，可检索到刚写入的记忆。
2. PNG Data URL Add 成功，文本查询可以召回图片记忆。
3. 图片查询可以召回图片记忆。
4. 相同 `request_id` 重复 Add 返回一致结果，不重复写入。
5. `options` 字段和 `top_k=100` 请求正常。
6. 不同 `user_id` 之间保持隔离。
7. API/容器重启后，重启前写入的记忆仍可检索。
8. Smoke 产生的临时数据库记录和对象文件已通过清理脚本核对并删除。

## 容量验证

容量脚本对 Add/Search 分别执行并发 1、2、4、8 的小规模测试。所有请求均返回 HTTP 200，
契约检查通过且无请求错误。关键结果如下：

| 操作 | 并发 | P95 延迟 |
| --- | ---: | ---: |
| Add | 1 | 4.430 s |
| Add | 2 | 4.964 s |
| Add | 4 | 33.774 s |
| Add | 8 | 5.601 s |
| Search | 1 | 0.421 s |
| Search | 2 | 0.370 s |
| Search | 4 | 0.461 s |
| Search | 8 | 0.831 s |

并发 4 的 Add 出现一次明显长尾，因此本候选采用保守运行边界：

- 建议 Add 并发不超过 4，客户端超时 300 秒；
- 建议 Search 并发不超过 8，客户端超时 60 秒；
- 建议每个 Key 不超过 60 请求/分钟，限流重试等待至少 1 秒；
- Provider 单次调用超时 30 秒，最多尝试 2 次。

测试后 16 组容量数据均已完成数据库和对象文件清理。资源采样显示 API 约 86 MiB、PostgreSQL
约 78–81 MiB、Caddy 约 47 MiB；本次小规模测试未出现内存压力。

## 备份与恢复演练

一致性备份目录：`/var/backups/masm/20260930T074114Z`。备份包含 PostgreSQL dump 和对象卷归档，
二者的 SHA-256 校验均通过。

| 数据项 | 数量 |
| --- | ---: |
| users | 4 |
| sessions | 3 |
| source_messages | 3 |
| memories | 3 |
| assets | 1 |
| memory_embeddings | 4 |
| request_ledger | 3 |
| 对象文件 | 1 |

该备份已恢复到隔离的临时 PostgreSQL、对象卷和 API 实例。恢复后的表计数、对象数、Health 和
既有标记检索均通过，随后删除临时恢复资源；生产实例在演练后继续返回健康状态。

## 运行加固

- PostgreSQL 与对象数据分别写入持久 Docker 命名卷。
- API 端口仅绑定 `127.0.0.1`，云安全组无需开放 8001。
- API、PostgreSQL、Caddy 均使用 Docker `json-file` 日志轮转：单文件 10 MiB，保留 5 个。
- 公网只开放 80/443；SSH 仅用于运维，不属于比赛 API。
- 日志与公开错误响应不记录密钥、原始比赛载荷、图片 Base64、Prompt 或 Provider 响应正文。

## 提交前剩余事项

1. 将服务器 Git 工作区切换到 `submission-rc2` 并从该标签重建 API 镜像。
2. 记录重建后的 Git commit、镜像摘要和 Compose 状态，确认三者一致。
3. 再执行一次 Health、鉴权、Add/Search 和重启持久性验收。
4. 使用比赛专用 Memory System Key 提交 Evaluation Access Request。

