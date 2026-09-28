"""Embedding Provider 接口。"""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbeddingProvider(ABC):
    """文本与图片向量 Provider。

    所有向量都必须在同一模型空间内产生，并记录模型名、版本与维度，
    存储与检索据此严格隔离向量空间。
    """

    model_name: str
    model_version: str
    dimensions: int

    @abstractmethod
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量生成文本向量，返回顺序与输入一致。"""

    @abstractmethod
    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        """批量生成图片向量（输入为解码后的图片字节），返回顺序与输入一致。"""
