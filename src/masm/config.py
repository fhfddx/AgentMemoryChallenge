"""应用配置。

密钥只通过环境变量注入；仓库只提交 `.env.example`，绝不将密钥写入源码。
"""

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    """读取整型环境变量；未设置或为空时返回默认值。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """MASM 服务配置。

    仅包含公共配置；连接串与密钥通过环境变量注入，绝不进入源码或日志。
    """

    database_url: str
    api_keys: tuple[str, ...] = ()
    max_image_bytes: int = 10485760  # 单图解码后上限 10 MiB
    max_add_image_bytes: int = 31457280  # 单次 Add 图片总量上限 30 MiB
    max_search_response_bytes: int = 31457280  # Search 响应总量上限 30 MiB
    max_top_k: int = 100

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构建配置。"""
        database_url = os.getenv("DATABASE_URL", "")
        api_keys = tuple(key for key in os.getenv("MASM_API_KEYS", "").split(",") if key)
        return cls(
            database_url=database_url,
            api_keys=api_keys,
            max_image_bytes=_int_env("MASM_MAX_IMAGE_BYTES", 10485760),
            max_add_image_bytes=_int_env("MASM_MAX_ADD_IMAGE_BYTES", 31457280),
            max_search_response_bytes=_int_env("MASM_MAX_SEARCH_RESPONSE_BYTES", 31457280),
            max_top_k=_int_env("MASM_MAX_TOP_K", 100),
        )
