# Agent Memory Challenge 提交检查表

## 代码与版本

- [ ] 公开仓库可匿名克隆，README 的安装和部署命令可从空环境执行。
- [ ] 提交使用固定 Git commit，并记录 `submission-rc1` 或最终标签对应的 commit。
- [ ] 镜像摘要、线上配置和 Git commit 属于同一构建。
- [ ] `THIRD_PARTY_NOTICES.md` 已补全最终依赖、论文、许可证和修改说明。
- [ ] 仓库不包含 `.env`、API Key、数据库口令、模型密钥、原始评测数据或运行产物。

## 接口与认证

- [ ] API 地址和 HTTPS 证书有效。
- [ ] `GET /health` 无认证返回 2xx，且只返回 `{"status":"ok"}`。
- [ ] `/add`、`/search` 的 Bearer/Token/X-Api-Key 认证与官方调用方式一致。
- [ ] Add/Search Schema、错误码和 `top_k=100` 上限与冻结候选版本一致。
- [ ] 内部依赖探针不作为公共路由暴露，输出中没有连接串、路径、异常或密钥。

## 容量、超时和持久性

- [ ] 单图 10 MiB、单 Add 图片 30 MiB、Search 响应 30 MiB 的边界已验证。
- [ ] API、数据库、模型调用和反向代理超时已记录，并与比赛平台限制兼容。
- [ ] PostgreSQL 和图片对象使用持久卷；备份/恢复演练覆盖两者的一致快照。
- [ ] API 容器重启后，重启前 Add 的文本和图片仍可 Search。
- [ ] 当前单机文件对象存储限制已获接受；若改为外部 S3，重新验证 staging、发布和删除语义。

## 验证证据

- [ ] `pytest -v` 全部通过。
- [ ] `ruff check .` 与 `mypy src` 全部通过。
- [ ] 空数据库执行 `alembic upgrade head` 成功。
- [ ] `docker compose up -d --build` 后 Health/Add/Search/重复 Add Smoke 通过。
- [ ] `docker compose restart api` 后持久性 Smoke 通过。
- [ ] 全路径用户隔离测试通过；日志抽检不含原文、Base64、API Key 或 Prompt。
- [ ] B0、B1、MASM 和消融实验清单包含 commit、模型、Prompt、数据集、seed 与成本。
