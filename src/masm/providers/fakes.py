"""确定性的 Fake Embedding Provider（测试与本地开发使用，不访问付费 API）。"""

import hashlib
import math
import re
from collections.abc import Sequence

from masm.providers.embeddings import EmbeddingProvider

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+")

# 图片特征抽样上限，避免对超大图片做线性扫描。
_MAX_SAMPLES = 4096


class DeterministicFakeEmbeddingProvider(EmbeddingProvider):
    """基于词袋哈希与字节分布抽样的确定性向量 Provider。

    相同输入永远得到相同向量；共享词或字节分布相近的输入得到相近向量，
    足以支撑基线 Add→Search 集成与跨用户隔离测试。
    """

    model_name = "fake-multimodal"
    model_version = "v1"

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 1:
            raise ValueError("dimensions 必须为正整数")
        self.dimensions = dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._text_vector(text) for text in texts]

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        return [self._byte_vector(image) for image in images]

    def _text_vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            values[int.from_bytes(digest, "big") % self.dimensions] += 1.0
        return _normalize(values, self.dimensions)

    def _byte_vector(self, data: bytes) -> list[float]:
        values = [0.0] * self.dimensions
        length = len(data)
        step = max(1, length // _MAX_SAMPLES)
        for index in range(0, length, step):
            values[(index + data[index]) % self.dimensions] += 1.0
        return _normalize(values, self.dimensions)


def _normalize(values: list[float], dimensions: int) -> list[float]:
    """单位化向量；空输入返回均匀单位向量，避免 pgvector 余弦距离退化。"""
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        uniform = 1.0 / math.sqrt(dimensions)
        return [uniform] * dimensions
    return [value / norm for value in values]
