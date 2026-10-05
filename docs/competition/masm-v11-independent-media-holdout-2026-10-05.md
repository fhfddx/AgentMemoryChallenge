# MASM v1.0 / v1.1 独立媒体小样本运行前协议

日期：2026-10-05。用途仅为本机候选决策，不是官方评测、隐藏盲测或容量测试。

## 1. 已冻结输入

- 样本数：3；各 1 次含真实图片的 Add、1 次纯文本 Search。
- 清单：`external-cc0-media-v1.json`，仅保存在本机临时目录，不纳入 Git。
- 清单 SHA-256：`a8b445f9c13b6dd67fed596aa2fe5d1072deef7388a55bd3145ce020314be216`。
- 选择规则：在两版运行前固定 1 张动物、1 张水果静物、1 张街景，三者视觉主题不同；不得按结果换图、改问题或改答案标记。
- 图片格式均为 JPEG，单张小于 10 MiB。报告只保存公开来源、许可、文件哈希、无语义 ID 和聚合指标，不保存图片、Data URI、查询或答案。

| ID | 公开来源 | 许可 | 本地文件 SHA-256 | 事前问题 | 原子答案标记 |
| --- | --- | --- | --- | --- | --- |
| commons-image-001 | Wikimedia Commons `Crocodylus acutus in La Manzanilla.jpg` | CC0-1.0 | `e42d1221b51f4a6e238a02879849512581cc8cf649529e48d62dd50344b5e6bb` | Which animal is partly submerged in the water? | `crocodile` |
| commons-image-002 | Wikimedia Commons `Collection of fruits (Unsplash).jpg` | CC0-1.0 | `1c825d948f98c0693d742ef377531b30110ae2620b66cfcc22bfc3915bc37ecb` | Which fruit has jewel-like red seeds in the image? | `pomegranate` |
| commons-image-003 | Wikimedia Commons `Street photography.jpeg` | CC0-1.0 | `1a9c829276f32b70f2beb2ce9c68e21f109f78fc3f3e97504e7187df0696f09a` | What kind of street activity are people gathered around? | `vendor` |

公开来源：

- https://commons.wikimedia.org/wiki/File:Crocodylus_acutus_in_La_Manzanilla.jpg
- https://commons.wikimedia.org/wiki/File:Collection_of_fruits_(Unsplash).jpg
- https://commons.wikimedia.org/wiki/File:Street_photography.jpeg

## 2. 固定运行条件

- v1.0 源：主仓固定提交 `af9173f939b652443be4f27ef438bb1d6e79e8cf`；已验证它与 `submission-rc2` 在 `src/`、`alembic/`、`pyproject.toml`、`Dockerfile` 无差异。
- v1.1 源：现有 `codex/masm-v11` 工作树；运行时记录当前提交与工作树状态，不把提交号误写成包含未提交改动的完整身份。
- 两版运行档位均为 `official-masm`，融合路径保持关闭；使用相同清单、哈希、模型配置和本机硬件。
- 两版使用不同的临时 PostgreSQL 容器、数据库与资产目录；v1.0 迁移到自身 `0003` head，v1.1 迁移到自身 `0005` head。每版完成后检查四张业务表、资产目录及清理清单。
- 先跑 v1.0；只在其成功、usage 完整且清理完成后跑 v1.1。
- 每版 `--max-llm-attempts 18 --max-embedding-attempts 18`，含失败重试；触顶即停止并精确清理。该上限不是货币账单上限。
- 费用只记官方原价估算，OpenAI 美元与百炼人民币分开，不合并；实际账单继续标为 `not_reconciled`，不再追查 OpenAI 细分账单。

## 3. 事前判定门槛

每个 case 的 `recall_at_10=1` 表示 Search 证据中逐字出现该 case 的唯一原子标记。n=3 太小，P95 只作描述，不作统计显著性结论。

只有同时满足下列条件，独立媒体证据才支持 `v1.1 go`：

1. v1.1 至少比 v1.0 多命中 1 个 case（绝对差至少 1/3）；
2. v1.1 在其余 case 不回退；
3. 两版无 Provider 失败、usage 缺失、跨用户泄漏或清理失败；
4. v1.1 Add P95 不高于 v1.0 的 1.25 倍；若超过，只能在质量提升明确且延迟风险有解释时人工复核，不能自动 go；
5. v1.1 没有启用实验融合路径，报告中的图片尝试和逻辑调用与固定档位一致。

若两版均满分、差异不足 1/3、任一安全/清理条件失败，或结果无法复现，则该样本不提供 v1.1 的独立正向证据，按既定 Gate 1 选择 `v1.0 fallback`。不得事后更换问题、答案词或图片来制造差异。

## 4. 执行边界

运行前必须先通过加载器单元测试、Fake Provider 与隔离数据库清理检查。真实 Provider 只允许这一轮配对小样本；不得运行官方 Smoke/Full、容量测试、云服务器操作或生产数据库访问。任何密钥只从本机 `.env` 限定字段载入当前进程，不打印、不写报告。
