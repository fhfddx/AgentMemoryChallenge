# 正式模型 Provider 配置指南

> 核对日期：2026-09-29。价格和可用区域可能变化，正式实验前应再次查看官方页面。

## 推荐组合

| 用途 | Provider | 模型 | 原因 |
|---|---|---|---|
| 结构化推理与图片感知 | OpenAI API | `gpt-4o-mini` | 原生支持文本、图片输入和 Structured Outputs，与当前 `/chat/completions` 适配器一致。 |
| 统一文本向量空间 | 阿里云百炼 Model Studio | `text-embedding-v4`，1024 维 | 提供 OpenAI 兼容 `/embeddings` 接口，支持 1024 维和中英文检索。 |

官方资料：

- [OpenAI：GPT-4o mini 模型](https://developers.openai.com/api/docs/models/gpt-4o-mini)
- [阿里云百炼：OpenAI 兼容 Embedding 接口](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai)
- [阿里云百炼：text-embedding-v4 模型信息](https://help.aliyun.com/zh/model-studio/text-embedding-v4)

## 本地配置

在仓库根目录复制 `.env.example` 为 `.env`，再修改以下字段。不要把真实密钥发到聊天、截图或提交到 Git。

```dotenv
MASM_RUNTIME_PROFILE=official-baseline

MASM_LLM_BASE_URL=https://api.openai.com/v1
MASM_LLM_API_KEY=<OPENAI_API_KEY>
MASM_LLM_MODEL=gpt-4o-mini

MASM_EMBEDDING_BASE_URL=https://<WORKSPACE_ID>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
MASM_EMBEDDING_API_KEY=<DASHSCOPE_API_KEY>
MASM_EMBEDDING_MODEL=text-embedding-v4
MASM_EMBEDDING_DIMENSIONS=1024

MASM_MODEL_TIMEOUT_SECONDS=30
MASM_MODEL_MAX_ATTEMPTS=2
```

`WORKSPACE_ID`、API Key 和模型必须属于同一百炼地域。若使用新加坡地域，应采用控制台给出的新加坡 OpenAI 兼容 Base URL，不要混用北京地域的 Key。

## 验收顺序

1. 先使用 `official-baseline` 执行一组文本和真实图片 Smoke，验证两个 Provider、向量维度与跨模态召回。
2. 记录 OpenAI 模型快照、百炼地域/模型版本、调用次数、延迟和实际费用。
3. 再切换为 `official-masm`，验证 Perception、Temporal、Curator 三智能体及查询分析链路。
4. 将真实 Provider 版本写入 `experiments/configs/b0.yaml` 与 `experiments/configs/masm.yaml`，替换 `record-before-run`。
5. 两个档位均通过后，再运行小规模 B0/MASM 对比实验。

## 费用边界

OpenAI 官方当前列出的 `gpt-4o-mini` 标准文本价格为每百万输入 token 0.15 美元、每百万输出 token 0.60 美元。百炼当前列出的北京地域 `text-embedding-v4` 文本输入原价为每百万 token 0.5 元。首次 Smoke 应只使用少量样例，并在控制台设置消费限额。

## 仍需比赛方确认

- 是否允许 LLM 与 Embedding 使用不同的官方 Provider；
- 图片先由 `gpt-4o-mini` 结构化，再进入 `text-embedding-v4` 是否符合多模态方法组规则；
- 提交时是否必须固定到 `gpt-4o-mini-2024-07-18` 快照，还是允许使用 `gpt-4o-mini` 别名。
