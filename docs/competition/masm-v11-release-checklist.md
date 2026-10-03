# MASM v1.1 独立发布清单（待批准执行）

本清单是发布门槛，不是已执行记录。当前仅完成本地代码、假模型合成对照与配置验证；
不得把它们当作正式模型成绩。线上 v1.0 `submission-rc2`、其数据库、资产卷和现有域名
`agentmemorydev.icu` 必须保持原状。是否上线新版本及是否使用官方 Smoke，需在审阅正式
模型小样本、容量、时延和成本后由用户决定。

## 1. 上线前核对

- [ ] 确认分支提交 SHA、测试总数、迁移头 `0005`、正式配置
  `official-masm / gpt-4o-mini / text-embedding-v4 / 1024`；记录 v1.0 当前 SHA、镜像
  digest 和配置摘要，不把密钥复制进记录。
- [ ] 对 v1.0 PostgreSQL 和对象卷做同一时点备份、校验 SHA256，并在隔离环境演练恢复；
  v1.1 使用另一个数据库和资产卷，绝不把迁移直接指向 v1.0 数据库。
- [ ] 以只含占位值的环境运行 `docker compose config -q`、
  `docker compose -p masm-v11 -f deployments/docker-compose.v11.yml config -q`，并验证
  Caddyfile。检查解析后的 v1.0 路由仍为 `api:8000`，v1.1 为 `masm-v11-api:8000`。
- [ ] 准备仅供 v1.1 使用的 `MASM_V11_POSTGRES_PASSWORD`、`MASM_V11_API_KEYS`、
  `MASM_V11_RUNTIME_PROFILE=official-masm`；沿用已合规的 LLM/Embedding 供应商配置，
  但不要在命令、日志或文档中打印密钥。前两项在 Compose 中是必填项、没有默认值；
  数据库口令进入连接 URL，需使用 URL 安全随机字符（字母、数字、`-`、`_`）。
- [ ] 给 `v11.agentmemorydev.icu` 添加指向服务器公网 IP 的 DNS，待公网解析成功。
  新域名仅用于并行候选，旧域名不切换。

## 2. 隔离启动顺序

1. 确认 `masm-edge` 网络不存在或属于本项目；必要时创建一次。旧 Compose 的 Caddy
   与新 Compose 的 API 通过该外部网络通信，新 Postgres 不接入边缘网络。
2. 先启动 `masm-v11` Compose 的 API/Postgres。它只在宿主机 `127.0.0.1:8002`
   暴露调试端口，Postgres 不暴露端口；等待迁移成功和容器 Health healthy。
3. 对 `http://127.0.0.1:8002/health` 做本机检查，再仅重建旧项目的 Caddy 容器以加载
   第二站点及边缘网络。不要重建或替换 v1.0 API/Postgres。
4. 从外网分别核对 `https://agentmemorydev.icu/health` 与
   `https://v11.agentmemorydev.icu/health`，均应返回 `{"status":"ok"}`；检查两张证书
   的域名和有效期。v1.0 原域名必须始终可用。

## 3. 正式模型小样本与容量门槛

- [ ] v1.1 独立 Key 做文本、图文、纯图片 Add/Search/重启持久性 Smoke，核对真正的
  消息来源与上下文、HTTP 401、幂等重试和运行级删除；测试运行的数据库行、向量、
  资产对象全部清理，不能碰 v1.0 或其他用户。
- [ ] 在 v1.0/v1.1 使用同一预注册小样本与正式模型重跑对照，分别报告标准
  Recall@10/100、原子证据覆盖率、返回条数、P95 延迟、响应字节、Embedding 输入量、
  LLM/感知调用数和费用；未观测到的指标标为 `unavailable`，不估算。
- [ ] 验证并发 Add 16、Search 16 的错误率、P95 和内存/CPU；复核 30 MiB 响应上限、
  Provider 最多 64 条重排输入、最终 Top K 100。
- [ ] 若覆盖收益不足、延迟/成本过高、图片感知增加 LLM 调用，或存在数据泄露/跨用户
  问题，停止发布并回到本地，不创建新的官方版本。

## 4. 回滚与官方提交

- [ ] 回滚只撤下 v1.1 站点或停止 `masm-v11` API；保留 v1.1 数据卷以便诊断和恢复，
  不使用 `down -v`。旧域名和旧 API/Postgres 不应受到回滚影响。
- [ ] 只有用户审阅正式模型对照、成本与容量记录并明确同意后，才在官网添加 v1.1
  Version、填写独立 URL/Key 并运行一次官方 Smoke。当前不进行此步，也不上传密钥。
- [ ] 上线后检查 HTTPS、401、容器 Health、日志轮转 `10m × 5`、磁盘/证书到期，
  以及备份可恢复性；发生异常按上一条回滚。

本机合成对照和局限见 `masm-v11-local-synthetic-report.md`。
