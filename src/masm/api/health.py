"""部署依赖探针；输出只含固定枚举状态，不包含异常或配置值。"""

from typing import Protocol

from sqlalchemy import Connection, text


class _ConnectableEngine(Protocol):
    def connect(self) -> Connection: ...


class _DatabaseLike(Protocol):
    engine: _ConnectableEngine


class _AssetStoreLike(Protocol):
    def check_ready(self) -> None: ...


def probe_dependencies(database: _DatabaseLike, asset_store: _AssetStoreLike) -> dict[str, object]:
    """检查数据库和对象目录，只返回 ``ok/unavailable``，绝不返回异常文本。"""
    states = {"asset_store": "ok", "database": "ok"}
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        states["database"] = "unavailable"

    try:
        asset_store.check_ready()
    except Exception:
        states["asset_store"] = "unavailable"

    status = "ready" if all(value == "ok" for value in states.values()) else "degraded"
    return {"status": status, "dependencies": states}
