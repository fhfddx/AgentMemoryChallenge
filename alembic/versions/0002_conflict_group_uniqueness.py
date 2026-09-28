"""add conflict group uniqueness constraints

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

冲突组成员与版本号必须在数据库层唯一：应用层的行锁负责串行化，唯一约束负责兜底，
两者共同保证并发追加既不拆组也不产生重复版本号。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_memory_conflicts_group_memory",
        "memory_conflicts",
        ["user_id", "conflict_group_id", "memory_id"],
    )
    op.create_unique_constraint(
        "uq_memory_conflicts_group_version",
        "memory_conflicts",
        ["user_id", "conflict_group_id", "version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_memory_conflicts_group_version", "memory_conflicts", type_="unique"
    )
    op.drop_constraint(
        "uq_memory_conflicts_group_memory", "memory_conflicts", type_="unique"
    )
