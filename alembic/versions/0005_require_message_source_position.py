"""require source position for message memories

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_memories_granularity_position", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_granularity_position",
        "memories",
        "(granularity = 'context' AND source_position IS NULL) OR "
        "(granularity = 'message' AND source_position IS NOT NULL AND source_position >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memories_granularity_position", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_granularity_position",
        "memories",
        "(granularity = 'context' AND source_position IS NULL) OR "
        "(granularity = 'message' AND source_position >= 0)",
    )
