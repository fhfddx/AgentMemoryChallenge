"""数据库连接与会话工厂。"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """封装 SQLAlchemy 引擎与会话工厂。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @classmethod
    def create(cls, database_url: str) -> "Database":
        """根据连接串创建 Database。"""
        return cls(create_engine(database_url))

    @property
    def engine(self) -> Engine:
        """SQLAlchemy 引擎。"""
        return self._engine

    def session(self) -> Session:
        """创建新的会话（上下文管理器）。"""
        return self._session_factory()
