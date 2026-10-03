"""memory granularity and source position

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("granularity", sa.String(length=16), server_default="context", nullable=False),
    )
    op.add_column("memories", sa.Column("source_position", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_memories_granularity_position",
        "memories",
        "(granularity = 'context' AND source_position IS NULL) OR "
        "(granularity = 'message' AND source_position >= 0)",
    )
    op.create_index(
        "ux_memories_context_per_run",
        "memories",
        ["user_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("granularity = 'context'"),
    )
    op.create_index(
        "ux_memories_message_per_position",
        "memories",
        ["user_id", "request_id", "source_position"],
        unique=True,
        postgresql_where=sa.text("granularity = 'message'"),
    )


def downgrade() -> None:
    op.drop_index("ux_memories_message_per_position", table_name="memories")
    op.drop_index("ux_memories_context_per_run", table_name="memories")
    op.drop_constraint("ck_memories_granularity_position", "memories", type_="check")
    op.drop_column("memories", "source_position")
    op.drop_column("memories", "granularity")
