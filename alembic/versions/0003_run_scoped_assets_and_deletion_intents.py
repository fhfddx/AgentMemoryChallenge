"""run scoped assets and persistent deletion intents

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

为运行级删除提供两个持久化基础：
1. `assets.request_id`：确立「资产属于哪一次运行」，使共享内容寻址对象的删除
   只作用于目标运行；
2. `deletion_intents`：把对象删除意图持久化，保证对象删除失败后可跨进程幂等重试。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("request_id", sa.String(length=255), nullable=True))
    op.create_index("ix_assets_request_id", "assets", ["request_id"])

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
