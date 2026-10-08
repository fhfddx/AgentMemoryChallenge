# MASM v1.1 云端 Smoke 恢复与清理报告（2026-10-08）

## 结论

2026-10-07 的多模态 Smoke 在写入 55.9% 时停滞，任务随后由用户取消。本轮对该任务留下的数据做了只读审计和精确清理。清理范围来自受保护清单，共 19 个已提交运行；清理完成后，运行范围内的账本、源消息、记忆、资产和处理记录均为 0，对象删除没有失败，删除意图也没有 `PENDING`。

这份结果只说明卡住任务的数据已经清干净，公网服务当时处于健康状态。官方 Smoke 尚未重新运行，因此不能据此宣称 v1.1 已通过官方 Smoke 或可以直接提交 Full 评测。

## 边界

- 操作对象只有 v1.1 容器 `masm-v11-api-1`、`masm-v11-postgres-1` 及其资产目录。
- 没有修改或删除 v1.0 数据库卷、资产卷和容器。
- 本轮恢复阶段没有新启动官方 Smoke、Full 评测或容量测试。
- 没有合并 `main`。
- 报告不记录 API Key、数据库连接串、原始用户标识、请求标识或对象 URI。

## 恢复工具与代码状态

新增工具：`scripts/cloud_smoke_recovery.py`。

工具把审计与清理分成两个显式子命令：

- `audit` 使用 PostgreSQL `REPEATABLE READ`、`READ ONLY` 事务读取指定时间窗，只把原始作用域写入权限为 `0600` 的清单；标准输出仅包含哈希和汇总数据。
- `cleanup` 要求清单行数与数字确认完全一致，逐运行调用现有 `DeletionService`，随后再次统计五张运行范围表。

本机验证结果：

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

## 尚未验证

1. 清理完成后尚未重新创建官方 Smoke 任务。写入阶段能否完整越过先前的 55.9%，仍需用一次新的官方 Smoke 验证。
2. 当前服务器运行镜像在本轮没有重建。`bbae82c` 的脱敏供应商失败诊断已经进入公开分支，但本报告不把它记作已部署运行时能力。
3. 尚未运行 Full 评测，也没有提交最终评测结果。

下一步应先部署固定候选镜像，并在不改变声明并发上限的前提下运行一次新的官方 Smoke。只有 Smoke 完整通过、其测试数据按任务清单清理后，才进入 Full 评测提交。
