"""初始记忆数据模型（设计规范 7.1 的 12 张必需表）。

以 models.py 的元数据为唯一来源，避免迁移与模型不一致；先创建 pgvector 扩展。
"""

from alembic import op

from masm.storage.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 pgvector 扩展和全部数据表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """删除全部数据表。"""
    Base.metadata.drop_all(bind=op.get_bind())
