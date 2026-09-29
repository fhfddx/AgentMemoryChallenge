"""图片经忠实感知后进入文本向量空间的测试。"""

import io
import json

import httpx
import pytest
from PIL import Image

from masm.agents.perception import PerceptionAgent
from masm.providers.errors import ProviderResponseError, ProviderUnavailableError
from masm.providers.fakes import FakeStructuredLLM
from masm.providers.multimodal_embeddings import (
    GroundedMultimodalEmbeddingProvider,
    canonical_perception_text,
)
from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider
from masm.schemas.agents import EntityKind, PerceptionResult


def _image(format_name: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), (120, 30, 220)).save(buffer, format=format_name)
    return buffer.getvalue()


def _perception(label: str = "purple square") -> dict:
    return {
        "descriptions": [
            {"description": label, "ocr_text": "HELLO", "confidence": 0.9}
        ],
        "entities": [
            {
                "name": "square",
                "kind": EntityKind.OBJECT.value,
                "confidence": 0.8,
                "evidence": label,
            }
        ],
        "keywords": ["purple", "shape"],
        "language": "en",
        "modality": "image",
    }


def _text_provider(seen: list[dict] | None = None) -> OpenAICompatibleEmbeddingProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index + 1), 0.5]}
                    for index, _ in enumerate(body["input"])
                ]
            },
        )

    return OpenAICompatibleEmbeddingProvider(
        model="text-embedding-v4",
        model_version="cycle2-fixed",
        dimensions=2,
        base_url="https://embeddings.invalid/v1",
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=1,
    )


def _provider(
    responses: list[dict | Exception], seen: list[dict] | None = None
) -> tuple[GroundedMultimodalEmbeddingProvider, FakeStructuredLLM]:
    llm = FakeStructuredLLM(responses, model="gpt-4o-mini")
    provider = GroundedMultimodalEmbeddingProvider(
        text_embeddings=_text_provider(seen),
        perception=PerceptionAgent(llm),
    )
    return provider, llm


def test_texts_delegate_to_text_embedding_provider() -> None:
    seen: list[dict] = []
    provider, llm = _provider([], seen)

    vectors = provider.embed_texts(["alpha", "beta"])

    assert vectors == [[1.0, 0.5], [2.0, 0.5]]
    assert seen[0]["input"] == ["alpha", "beta"]
    assert llm.requests == []


@pytest.mark.parametrize(
    ("format_name", "media_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_png_jpeg_and_webp_are_detected_from_decoded_bytes(
    format_name: str, media_type: str
) -> None:
    provider, llm = _provider([_perception()])

    provider.embed_images([_image(format_name)])

    content = llm.requests[0].content
    assert content is not None
    url = content[0].image_url.url  # type: ignore[union-attr]
    assert url.startswith(f"data:{media_type};base64,")


def test_images_are_sent_as_ordered_data_uris_then_embedded_as_canonical_text() -> None:
    seen: list[dict] = []
    provider, llm = _provider(
        [_perception("first image"), _perception("second image")], seen
    )

    vectors = provider.embed_images([_image("PNG"), _image("JPEG")])

    assert vectors == [[1.0, 0.5], [2.0, 0.5]]
    assert len(llm.requests) == 2
    assert seen[0]["input"] == [
        "description: first image\nocr: HELLO\nentities: square\nkeywords: purple, shape",
        "description: second image\nocr: HELLO\nentities: square\nkeywords: purple, shape",
    ]


def test_text_and_image_vectors_share_model_version_and_dimensions() -> None:
    provider, _llm = _provider([_perception()])

    assert provider.model_name == "text-embedding-v4"
    assert provider.model_version == "cycle2-fixed"
    assert provider.dimensions == 2
    assert len(provider.embed_texts(["text"])[0]) == 2
    assert len(provider.embed_images([_image("PNG")])[0]) == 2


@pytest.mark.parametrize("data", [b"not-an-image", pytest.param(_image("GIF"), id="gif")])
def test_invalid_or_unsupported_image_bytes_are_rejected_without_network_call(
    data: bytes,
) -> None:
    provider, llm = _provider([])

    with pytest.raises(ProviderResponseError):
        provider.embed_images([data])

    assert llm.requests == []


def test_image_perception_failure_never_falls_back_to_fake_vectors() -> None:
    provider, _llm = _provider([ProviderUnavailableError("provider down")])

    with pytest.raises(ProviderUnavailableError):
        provider.embed_images([_image("PNG")])


def test_canonical_perception_text_rejects_empty_observation() -> None:
    with pytest.raises(ProviderResponseError):
        canonical_perception_text(PerceptionResult(modality="image"))
