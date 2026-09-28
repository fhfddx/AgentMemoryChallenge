"""run scoped assets and persistent deletion intents

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

为运行级删除提供两个持久化基础：
1. `assets.request_id`：确立「资产属于哪一次运行」，使共享内容寻址对象的删除
   只作用于目标运行；列新增后必须**回填历史数据**，否则旧资产行的 request_id 为
   NULL，运行级删除与「独占对象」判定都会失效；
2. `deletion_intents`：把对象删除意图持久化，保证对象删除失败后可跨进程幂等重试。

回填规则（确定性）：按 `source_messages` 中同用户消息的 image_url 反查对象地址；
若同一对象地址在多个运行中被引用，取字典序最小的 request_id 作为归属，保证迁移可重复。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 从 source_messages.content 里提取所有 image_url.url。
_URI_REFERENCES_SQL = """
    SELECT DISTINCT
        sm.user_id AS user_id,
        part->'image_url'->>'url' AS object_uri,
        sm.request_id AS request_id
    FROM source_messages AS sm
    CROSS JOIN LATERAL jsonb_array_elements(sm.content) AS part
    WHERE jsonb_typeof(sm.content) = 'array'
      AND part->>'type' = 'image_url'
      AND part->'image_url'->>'url' IS NOT NULL
"""

# 每个 (user_id, object_uri) 只保留字典序最小的归属运行（确定性，可重复执行）。
_RESOLVED_OWNERS_SQL = f"""
    SELECT refs.user_id, refs.object_uri, MIN(refs.request_id) AS request_id
    FROM ({_URI_REFERENCES_SQL}) AS refs
    GROUP BY refs.user_id, refs.object_uri
"""


def upgrade() -> None:
    op.add_column("assets", sa.Column("request_id", sa.String(length=255), nullable=True))
    op.create_index("ix_assets_request_id", "assets", ["request_id"])

    op.execute(
        f"""
        UPDATE assets AS a
        SET request_id = owners.request_id
        FROM ({_RESOLVED_OWNERS_SQL}) AS owners
        WHERE a.request_id IS NULL
          AND a.user_id = owners.user_id
          AND a.object_uri = owners.object_uri
        """
    )

    op.create_table(
        "deletion_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "request_id", "object_uri", name="uq_deletion_intents_scope_uri"
        ),
    )
    op.create_index("ix_deletion_intents_user_id", "deletion_intents", ["user_id"])
    op.create_index("ix_deletion_intents_request_id", "deletion_intents", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_deletion_intents_request_id", table_name="deletion_intents")
    op.drop_index("ix_deletion_intents_user_id", table_name="deletion_intents")
    op.drop_table("deletion_intents")
    op.drop_index("ix_assets_request_id", table_name="assets")
    op.drop_column("assets", "request_id")
