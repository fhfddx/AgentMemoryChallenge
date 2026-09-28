# 第三方组件与引用说明

本文件用于比赛提交审查，不替代各项目自带的许可证文本。最终镜像中的精确版本以
`pip freeze` 和容器镜像摘要为准。

| 项目 | 用途 | 许可证 | 本项目修改 |
| --- | --- | --- | --- |
| FastAPI | HTTP API | MIT | 未修改上游源码 |
| Pydantic | Schema 校验 | MIT | 未修改上游源码 |
| Uvicorn | ASGI 服务 | BSD-3-Clause | 未修改上游源码 |
| HTTPX | Smoke/实验 HTTP 客户端 | BSD-3-Clause | 未修改上游源码 |
| SQLAlchemy | ORM 与事务 | MIT | 未修改上游源码 |
| Alembic | 数据库迁移 | MIT | 未修改上游源码 |
| Psycopg 3 | PostgreSQL 驱动 | LGPL-3.0-only | 未修改上游源码 |
| pgvector-python | 向量类型集成 | MIT | 未修改上游源码 |
| Pillow | 图片解析与校验 | HPND | 未修改上游源码 |
| PostgreSQL | 关系数据库 | PostgreSQL License | 通过容器镜像使用 |
| pgvector | PostgreSQL 向量扩展 | PostgreSQL License | 通过容器镜像使用 |
| Caddy | 可选 HTTPS 反向代理 | Apache-2.0 | 仅提供 Caddyfile 配置 |

## 比赛与论文来源

- 项目接口与评测目标来自 Agent Memory Challenge Cycle 2 公布的比赛要求。
- 截至本候选版本，没有直接复制第三方论文仓库代码；算法与工程实现均位于本仓库。
- 后续若依据导师提供论文加入模型、Prompt、数据或代码，必须在提交前补充论文标题、作者、
  链接、许可证/使用条件、采用部分与本项目修改内容。

依赖许可证如与本摘要不一致，以相应发行包和上游仓库附带的许可证为准。
