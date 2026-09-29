# MASM 可复现实验

实验运行器只通过公开的 `POST /add` 和 `POST /search` 访问被测系统，不导入线上
Repository、ORM 模型或内部答案。配置文件使用 JSON 语法保存为 `.yaml`；JSON 是 YAML 1.2
的子集，因此无需新增解析依赖，也可被常见 YAML 工具读取。

## 配置矩阵

- `configs/b0.yaml`：`official-baseline` 正式模型基础检索。
- `configs/b1.yaml`：多通道混合检索基线。
- `configs/masm.yaml`：`official-masm` 完整 MASM。
- `configs/ablations/`：分别关闭 Temporal、Curator、关系扩展、图片通道、冲突重排，
  以及退化为单通道。

每个配置固定 seed、数据集版本、模型版本、Prompt 版本、部署 profile 和成本记账参数。
`git_commit: auto` 在运行时解析为当前 HEAD。B0 映射到 `official-baseline`，MASM 映射到
`official-masm`。`provider_revision: record-before-run` 是显式阻断标记：真实调用完成前必须
记录供应商版本或快照标识，不能把 Mock 或 `local-fake` 的结果写成正式实验。

## 运行

先启动与配置 `deployment_profile` 一致的服务、完成一次真实模型 Smoke，再设置 API Key：

```bash
set MASM_API_KEY=test-key
python experiments/run_experiment.py --config experiments/configs/b0.yaml --output artifacts/experiments/b0-smoke
```

Linux/macOS 使用 `export MASM_API_KEY=...`。输出目录包含 `manifest.json`，记录 Recall@10、
Recall@100、MRR、nDCG、平均/P95 延迟、成本、模型调用次数和降级率，但不包含记忆原文或查询原文。

成本、模型调用次数和降级数来自配置中由外部运行日志核对后的记账参数，因为公共 Add/Search
响应刻意不暴露内部遥测。正式报告不得保留默认占位值。

汇总多个运行：

```bash
python experiments/summarize_results.py \
  artifacts/experiments/b0/manifest.json \
  artifacts/experiments/b1/manifest.json \
  artifacts/experiments/masm/manifest.json \
  --output artifacts/experiments/comparison.json
```

## 接入公开基准

`adapters/atm_bench.py` 与 `adapters/mem_gallery.py` 接受规范化 JSON：`items` 保存公开 Add
载荷和只用于离线判定的 `match_token`，`queries` 保存 Search 查询与相关 item ID。原始公开数据
应离线转换为该格式；不要把受限制数据、个人信息或官方隐藏测试答案提交到仓库。
