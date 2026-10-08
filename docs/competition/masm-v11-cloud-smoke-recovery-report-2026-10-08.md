# MASM v1.1 云端 Smoke 恢复与清理报告（2026-10-08）

## 结论

2026-10-07 和 2026-10-08 的两次多模态 Smoke 都在写入 55.9% 时停滞，随后由用户取消。两次任务分别经过只读审计、清单备份和用户确认后，各精确清理了 19 个已提交运行。两轮清理的目标范围内均没有残留记录或待删除对象。

第二次停滞后的脱敏日志显示嵌入供应商反复返回 HTTP 400。代码原先把上下文摘要和全部消息摘要放在一次嵌入请求中，超过供应商同步接口每批最多 10 条文本的限制。候选版已按每批最多 10 条拆分请求并部署。一个 11 条消息的真实 Add/Search 探针返回 HTTP 200，Search 找到了最后一条消息；探针数据随后按固定运行标识清理。**修复后的官方 Smoke 尚未重跑**，因此这些结果不能当作官方 Smoke 或 Full 评测通过。

## 边界

- 操作对象只有 v1.1 容器 `masm-v11-api-1`、`masm-v11-postgres-1` 及其资产目录。
- 没有修改或删除 v1.0 数据库卷、资产卷和容器。
- 两次清理只针对各自清单中的 v1.1 运行；后续候选版部署只重建 v1.1 API，数据库容器 ID 未变。
- 修复后只执行了一个有界的 11 条消息 Add/Search 探针，没有启动官方 Full 评测或新的容量测试。
- 没有合并 `main`。
- 报告不记录 API Key、数据库连接串、正式评测的原始用户和请求标识，或对象 URI；文中的固定探针标识只对应我们自建的测试数据。

## 恢复工具与代码状态

新增工具：`scripts/cloud_smoke_recovery.py`。

工具把审计与清理分成两个显式子命令：

- `audit` 使用 PostgreSQL `REPEATABLE READ`、`READ ONLY` 事务读取指定时间窗，只把原始作用域写入权限为 `0600` 的清单；标准输出仅包含哈希和汇总数据。
- `cleanup` 要求清单行数与数字确认完全一致，逐运行调用现有 `DeletionService`，随后再次统计五张运行范围表。

首次恢复工具的本机验证结果：

- 完整测试集：`637 passed, 1 skipped, 92 warnings`。
- Windows 跳过的是 POSIX 文件权限用例；同一用例在无网络 Linux 容器内补测通过，输出为 `LINUX_MANIFEST_SAFETY_OK`。
- `ruff check .` 通过。
- `mypy src scripts/cloud_smoke_recovery.py` 通过，共检查 53 个源文件。

本地提交为 `32ae984febd49add97dd831c72ded81cab1771e0`。由于远端此前已有等价树但提交历史不同，公开分支通过 GitHub Git Data API 做了非强制更新，远端提交为 `563863d0861a187c13ea2f78e83a2959a24243c4`。远端树与本地提交树均为 `322398f25f421d94af42b318b561722747a7ce11`，三个新增文件的 blob 逐一匹配。

服务器从公开 `codex/masm-v11` 克隆代码，并核对 `HEAD` 为 `563863d0861a187c13ea2f78e83a2959a24243c4`。

## 只读审计

审计时间窗为北京时间 `[2026-10-07 19:15:00, 20:00:00)`，命令在 `masm-v11-api-1` 中运行。

审计结果：

| 项目 | 数量 |
| --- | ---: |
| 已提交运行 | 19 |
| 用户哈希分组 | 3（15、2、2） |
| `request_ledger` | 19 |
| `source_messages` | 90 |
| `memories` | 109 |
| `assets` | 12 |
| `processing_runs` | 0 |

清单信息：

- 容器路径：`/tmp/s.tsv`
- 主机备份：`/opt/masm-v11/recovery/smoke-20261007.tsv`
- 权限与所有者：`0600 root:root`
- 大小：2546 字节
- SHA-256：`9a1c7c4f3c068490258af0ca8a4e5a2ca81001cdda43a608686f26712f65f361`

容器内清单与主机备份的 SHA-256 相同。

## 精确清理

用户在删除动作发生前明确确认：删除清单中的 19 个 v1.1 Smoke 运行并执行零残留验证。

清理返回 `complete=true`、`failures=0`。汇总如下：

| 项目 | 数量 |
| --- | ---: |
| 运行 | 19 |
| 源消息 | 90 |
| 记忆 | 109 |
| 资产行 | 12 |
| 关系 | 11 |
| 冲突 | 0 |
| 已删除对象 | 12 |
| 缺失对象 | 0 |
| 保留共享对象 | 0 |

清理后，`assets`、`memories`、`processing_runs`、`request_ledger`、`source_messages` 在清单作用域内均为 0。删除意图的全库状态汇总为 `DONE|103`，没有 `PENDING`。

`complete=true` 还表示 `DeletionService` 没有留下失败对象 URI；本轮对象删除和暂存清理均已收敛。

## 服务健康检查

服务器容器状态：

- `masm-v11-api-1`：运行 18 小时，healthy。
- `masm-v11-postgres-1`：运行 2 天，healthy。
- `masm-caddy-1`：运行 45 小时。

本轮没有重建或重启这些容器。

从本机访问公网得到以下结果：

| 检查项 | `agentmemorydev.icu` | `v11.agentmemorydev.icu` |
| --- | --- | --- |
| DNS / 远端 IP | `124.243.186.95` | `124.243.186.95` |
| `GET /health` | HTTP 200，`{"status":"ok"}` | HTTP 200，`{"status":"ok"}` |
| TLS 校验 | `ssl_verify_result=0` | `ssl_verify_result=0` |
| 无鉴权 `POST /add` | HTTP 401 | HTTP 401 |
| 无鉴权 `POST /search` | HTTP 401 | HTTP 401 |

这些请求没有携带 API Key，也没有写入业务数据。

## 第二次 Smoke 与根因定位（2026-10-08）

第二次官方 Smoke 也停在 55.9%，用户将其取消。固定候选镜像 `masm-v11-final-candidate:563863d` 上的脱敏诊断持续记录 `provider.failed`，供应商为 `embedding`，错误类型为 `http_status`，状态码为 400。同一时段的 API 日志统计为：`POST /add` 19 次 HTTP 200、18 次 HTTP 503；另有 2 次 HTTP 401。先前的容量测试和容器健康检查没有复现写入停滞。

供应商的 `text-embedding-v4` 同步接口每次最多接受 10 条文本；原实现将上下文摘要与所有消息摘要一次提交。这个批量上限与脱敏 HTTP 400 相吻合。修复只把输入按 10 条拆批，保持顺序、原有 Add 原子性、声明并发和速率上限不变；没有截断消息或改动部署密钥。

第二次运行的只读审计时间窗为北京时间 `[2026-10-08 00:00, 2026-10-09 00:00)`。审计找到 19 个 `COMMITTED` 运行，分组为 2、15、2；对应 `request_ledger` 19、`source_messages` 90、`memories` 109、`assets` 12、`processing_runs` 0。清单容器路径为 `/tmp/smoke-current.tsv`，主机备份为 `/opt/masm-v11/recovery/smoke-20261008-1232.tsv`，两份 SHA-256 一致，均为 `0600 root:root`，大小 2546 字节。

用户明确确认删除这 19 个运行后，清理返回 `complete=true`、`failures=0`，删除 19 个运行关联的 90 条源消息、109 条记忆和 12 行资产，删除对象 12 个，没有缺失对象或保留的共享对象。独立 PostgreSQL 查询显示五张业务表全库行数均为 0，删除意图只有 `DONE|134`；资产目录文件数为 0。此时 v1.1 API 与 PostgreSQL 均为 healthy，Caddy 仍在运行。

## 修复版构建、部署和探针

批量修复的本地提交为 `233e041615990f5f1e4a6c2fa24b3b91aa451ffe`，公开分支上的等价提交为 `fc3e01377fd2075e0fb3c3eb790e6f19b73d6e2d`。新增回归用例验证 11 个输入按 10+1 拆分并保持输出顺序。本机完整测试为 `638 passed, 1 skipped, 92 warnings`，`ruff`、`mypy` 和 diff 检查通过。

服务器从公开分支克隆并核对了 `fc3e013` 的 HEAD、干净工作区及关键源文件和测试文件的 blob。构建镜像 `masm-v11-final-candidate:fc3e013` 后，无网络导入检查输出 `BATCH_LIMIT_OK 10`。部署使用独立 release 目录和镜像 override，仅执行 `up -d --no-deps api`。部署后 API 使用新镜像，状态为 running/healthy、重启计数为 0；v1.1 PostgreSQL 容器 ID 保持不变，旧 release 和镜像保留作回退。

部署后的有界探针使用 11 条消息发起一次 Add，随后 Search 最后一条消息的唯一标记。结果是 `ADD_HTTP 200`、`SEARCH_HTTP 200`、`MARKER_FOUND True`。它验证了真实供应商调用和超过单批上限的路径，但不替代官方 Smoke。

探针固定标识为 `batch-probe-fc3e013-20261008`，对应请求为 `batch-probe-fc3e013-20261008-add`。删除前只读预检返回 `TARGET_COMMITTED 1`；精确删除脚本返回 `complete=True`、`failed=0`、`memories=12`、`sources=11`、`assets=0`、`objects=0`。删除后又按这组固定标识独立查询 `request_ledger`、`source_messages`、`memories`、`assets` 和 `processing_runs`，五项均为 0，没有触发零残留断言。

本机在探针清理后复核公网：v1.0 与 v1.1 的 `/health` 均为 HTTP 200，TLS 校验结果为 0；v1.1 未携带密钥的 `/add` 和 `/search` 均为 HTTP 401。这些请求没有写入业务数据。

## 尚未验证

1. 在修复版上重新运行一次官方 Smoke，确认写入能完整越过此前的 55.9% 并取得最终结果；结束后再按确切清单审计和清理其测试数据。
2. 只有官方 Smoke 通过后，才考虑 Full 评测及最终提交。当前没有 Full 通过或提交结果。
