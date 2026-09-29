"""把图片忠实结构化后统一嵌入文本向量空间。"""

import base64
import io
from collections.abc import Sequence

from PIL import Image, UnidentifiedImageError

from masm.agents.perception import PerceptionAgent
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.errors import ProviderResponseError
from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider
from masm.schemas.agents import PerceptionResult
from masm.schemas.content import ImageURLPart

_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def canonical_perception_text(result: PerceptionResult) -> str:
    """把可观察感知字段转换为稳定、可嵌入的文本。"""
    lines: list[str] = []
    descriptions = [item.description.strip() for item in result.descriptions]
    ocr = [item.ocr_text.strip() for item in result.descriptions if item.ocr_text.strip()]
    entities = [item.name.strip() for item in result.entities]
    keywords = [item.strip() for item in result.keywords if item.strip()]
    if descriptions:
        lines.append(f"description: {'; '.join(descriptions)}")
    if ocr:
        lines.append(f"ocr: {'; '.join(ocr)}")
    if entities:
        lines.append(f"entities: {', '.join(entities)}")
    if keywords:
        lines.append(f"keywords: {', '.join(keywords)}")
    if not lines:
        raise ProviderResponseError("图片感知结果没有可嵌入的观察内容")
    return "\n".join(lines)


class GroundedMultimodalEmbeddingProvider(EmbeddingProvider):
    """用感知模型描述图片，再用文本 Provider 生成同空间向量。"""

    def __init__(
        self,
        *,
        text_embeddings: OpenAICompatibleEmbeddingProvider,
        perception: PerceptionAgent,
    ) -> None:
        self._text_embeddings = text_embeddings
        self._perception = perception
        self.model_name = text_embeddings.model_name
        self.model_version = text_embeddings.model_version
        self.dimensions = text_embeddings.dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return self._text_embeddings.embed_texts(texts)

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        descriptions: list[str] = []
        for image in images:
            part = ImageURLPart(image_url={"url": _data_uri(image)})
            descriptions.append(canonical_perception_text(self._perception.extract([part])))
        return self._text_embeddings.embed_texts(descriptions)


def _data_uri(data: bytes) -> str:
    """依据解码内容识别格式并生成内联 Data URI。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            format_name = image.format
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ProviderResponseError("图片字节无法识别") from exc
    media_type = _MEDIA_TYPES.get(format_name or "")
    if media_type is None:
        raise ProviderResponseError("图片格式不受支持")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"
