# MASM 本机媒体计量诊断设计

日期：2026-10-05。范围仅为 `scripts/metered_holdout.py` 的本机评估路径；
不改变正式 Add/Search、Provider、Prompt 或镜像。

## 目的与输入

新增内置 `media-v1` case set，从现有 `evaluate_synthetic_recall.build_cases`
固定选取 `text_image` 与 `image_only` 两例，使用同一 `run-tag` 生成的
8×8 PNG。前者检验图文 Add 与文本 Search，后者检验纯图 Add 与图片 Search。
每版仅 2 Add、2 Search，v1.0/v1.1 若比较必须同样本、同配置、不同用户 ID。
这是传输/计量与清理诊断，不是独立图像语义质量样本；`text_image` 查询
含其文本标记，不能据高 Recall 宣称图像识别正确。

## 计量与安全

沿用现有 HTTP 包装器，只保留 LLM 图片内容块数、图片相关实际尝试数、
usage 缺失、LLM/Embedding 总 tokens 与公开原价估算，不存图片/Data URI、
正文、密钥或单次 Provider 响应。图片专属 token/费用不可从整次 LLM
usage 拆分，报告始终标 `unavailable`，不得套用纯文本单独价格。
使用每版 Provider 尝试硬上限，先 v1.0，清理通过才考虑 v1.1。
数据库仅 localhost 的 `masm_test`，资产位于工作树 `.superpowers`；
运行前登记每个精确用户/请求，结束后核对四表与资产目录。

## 验收

先用单元测试锁定 `media-v1` 两例、两种查询形状和错误 case-set 行为；
再用 Fake Provider/隔离 PostgreSQL 集成测试验证图片 Add/Search 与
精确删除，包括资产文件零残留。所有离线测试与静态检查通过前不发起
正式模型媒体请求。实际 Provider 成功也只能证明这些小图可通过路径和
相关 HTTP usage 可观察，不能放行真实图片质量、成本或 Gate 2。
