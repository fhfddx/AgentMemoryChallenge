"""应用配置。

密钥只通过环境变量注入；仓库只提交 `.env.example`，绝不将密钥写入源码。
"""

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


def _int_env(name: str, default: int) -> int:
    """读取整型环境变量；未设置或为空时返回默认值。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    """读取浮点环境变量；未设置或为空时返回默认值。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


class RuntimeProfile(StrEnum):
    """MASM 的显式运行档位。"""

    LOCAL_FAKE = "local-fake"
    OFFICIAL_BASELINE = "official-baseline"
    OFFICIAL_MASM = "official-masm"


@dataclass(frozen=True)
class Settings:
    """MASM 服务配置。

    仅包含公共配置；连接串与密钥通过环境变量注入，绝不进入源码或日志。
    """

    database_url: str
    api_keys: tuple[str, ...] = ()
    asset_dir: Path = Path("artifacts/assets")
    max_image_bytes: int = 10485760  # 单图解码后上限 10 MiB
    max_add_image_bytes: int = 31457280  # 单次 Add 图片总量上限 30 MiB
    max_search_response_bytes: int = 31457280  # Search 响应总量上限 30 MiB
    max_top_k: int = 100
    runtime_profile: RuntimeProfile = RuntimeProfile.LOCAL_FAKE
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = "gpt-4o-mini"
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    model_timeout_seconds: float = 30.0
    model_max_attempts: int = 2
    fused_empty_history_text: bool = False

    @property
    def is_official(self) -> bool:
        """是否使用比赛正式模型档位。"""
        return self.runtime_profile is not RuntimeProfile.LOCAL_FAKE

    def validate_runtime(self) -> None:
        """在接收请求前校验运行档位与正式模型配置。"""
        if self.embedding_dimensions < 1:
            raise ValueError("Embedding 维度必须为正整数")
        if self.model_timeout_seconds <= 0:
            raise ValueError("模型超时必须为正数")
        if self.model_max_attempts not in {1, 2}:
            raise ValueError("模型尝试次数只能为 1 或 2")
        if not self.is_official:
            return

        required = (
            self.llm_base_url,
            self.llm_api_key,
            self.embedding_base_url,
            self.embedding_api_key,
        )
        if not all(value.strip() for value in required):
            raise ValueError("正式档位模型配置不完整")
        if self.llm_model != "gpt-4o-mini":
            raise ValueError("正式档位 LLM 必须为 gpt-4o-mini")
        if self.embedding_model != "text-embedding-v4":
            raise ValueError("正式档位 Embedding 必须为 text-embedding-v4")
        if self.embedding_dimensions != 1024:
            raise ValueError("正式档位 Embedding 维度必须为 1024")
        for name, value in (
            ("LLM", self.llm_base_url),
            ("Embedding", self.embedding_base_url),
        ):
            if urlparse(value).scheme.lower() != "https":
                raise ValueError(f"正式档位 {name} Base URL 必须使用 HTTPS")

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构建配置。"""
        database_url = os.getenv("DATABASE_URL", "")
        api_keys = tuple(key for key in os.getenv("MASM_API_KEYS", "").split(",") if key)
        return cls(
            database_url=database_url,
            api_keys=api_keys,
            asset_dir=Path(os.getenv("MASM_ASSET_DIR", "artifacts/assets")),
            max_image_bytes=_int_env("MASM_MAX_IMAGE_BYTES", 10485760),
            max_add_image_bytes=_int_env("MASM_MAX_ADD_IMAGE_BYTES", 31457280),
            max_search_response_bytes=_int_env("MASM_MAX_SEARCH_RESPONSE_BYTES", 31457280),
            max_top_k=_int_env("MASM_MAX_TOP_K", 100),
            runtime_profile=RuntimeProfile(
                os.getenv("MASM_RUNTIME_PROFILE", RuntimeProfile.LOCAL_FAKE.value)
            ),
            llm_base_url=os.getenv("MASM_LLM_BASE_URL", ""),
            llm_api_key=os.getenv("MASM_LLM_API_KEY", ""),
            llm_model=os.getenv("MASM_LLM_MODEL", "gpt-4o-mini"),
            embedding_base_url=os.getenv("MASM_EMBEDDING_BASE_URL", ""),
            embedding_api_key=os.getenv("MASM_EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("MASM_EMBEDDING_MODEL", "text-embedding-v4"),
            embedding_dimensions=_int_env("MASM_EMBEDDING_DIMENSIONS", 1024),
            model_timeout_seconds=_float_env("MASM_MODEL_TIMEOUT_SECONDS", 30.0),
            model_max_attempts=_int_env("MASM_MODEL_MAX_ATTEMPTS", 2),
            fused_empty_history_text=os.getenv("MASM_FUSED_EMPTY_HISTORY_TEXT") == "1",
        )
