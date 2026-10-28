# Full 评测失败说明（基础设施事故申诉）

**提交**：MASM v1.1（Open-source Methods · 多模态 · Cycle 2）
**部署版本**：`masm-v11-final-candidate:939c77f`（源码摘要 `e7839485…`）
**失败时间**：2026/10/28 16:06:35（平台记录）
**平台错误**：`ADD_SERVICE_UNAVAILABLE · HTTP 503`

---

## 一、根因

Full 运行期间，本次提交所依赖的 LLM 供应商（OpenAI 兼容接口，模型 `gpt-4o-mini`）**账户额度耗尽**，
返回：

```
HTTP 429
{"error":{"message":"You have no credits remaining. Add credits to continue using the API at
https://platform.openai.com/settings/organization/billing"}}
```

按设计，本服务在**依赖不可用**时统一返回 `HTTP 503 ADD_SERVICE_UNAVAILABLE`（响应体不含供应商正文，
且**不写入任何数据**），因此平台看到的是 503，而非能力评测失败。

## 二、证据

| 项目 | 观测 |
| --- | --- |
| 失败时段错误 | 仅 `POST /add` 返回 503；`POST /search` 无 503；`GET /health` 始终 200 |
| LLM 供应商 | 事故后即时探测：`429 no credits remaining`（1345 ms） |
| 向量供应商 | 同时刻探测：`200`（423 ms）——独立账户，全程正常 |
| 已提交数据 | `request_ledger` 中 `COMMITTED` = **3510** |
| 处理状态 | `processing_runs` **为空**，不存在半写或卡死记录 |
| 累计记忆 | 35,220 条（Full 前 12,353 条，本次新增约 2.3 万条） |

即：**额度耗尽前的 Add 全部成功且可查询；耗尽后的 Add 全部未写入**，不存在脏数据或部分处理。
服务在健康检查层面全程可用。

## 三、已采取的修复

1. 账户已充值；并将按 Full 实际量级（约为 Smoke 的 10–20 倍）预留足额配额，或切换到同一
   `gpt-4o-mini` 模型、具备充足配额的 OpenAI 兼容供应方。
2. 已把「运行前探测供应商余额与延迟」写入运维流程：`429 / 5xx / 超时` 时不启动评测。
3. 运行期间以 60 秒粒度监控 `POST /add` 的 5xx 与已写入条数，出现连续 5xx 立即处置。

## 四、请求

本次失败属于**第三方供应商配额耗尽**导致的服务不可用，而非方法/系统能力失败；数据无污染，
服务在恢复配额后可立即重新评测。

请告知：
1. 该失败尝试是否可以**替换为一次新的 Full 运行**（或该次是否占用两次 Full 名额之一）；
2. 若可以，建议的重跑时间窗口与是否需要在重跑前重新执行 Smoke。

我们会按贵方指示立即执行，不重复创建 Full 运行。
