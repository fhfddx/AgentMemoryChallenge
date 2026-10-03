"""把有序来源消息转换为不改写原文的消息级记忆。"""

from collections.abc import Sequence

from masm.providers.multimodal_embeddings import GroundedImageEmbedding
from masm.storage.types import EmbeddingDraft, MemoryDraft, SourceMessageDraft


def describe_message(
    message: SourceMessageDraft,
    image_evidence: Sequence[GroundedImageEmbedding],
) -> tuple[str, str, str]:
    """返回有序摘要、原始文本和模态；拒绝图片证据错位。"""
    summary_parts: list[str] = []
    text_parts: list[str] = []
    image_index = 0
    if isinstance(message.content, str):
        if message.content.strip():
            summary_parts.append(message.content)
            text_parts.append(message.content)
    else:
        for part in message.content:
            if part["type"] == "text":
                text = part["text"]
                if text.strip():
                    summary_parts.append(text)
                    text_parts.append(text)
            elif part["type"] == "image_url":
                if image_index >= len(image_evidence):
                    raise ValueError("图片证据数量少于来源图片")
                summary_parts.append(image_evidence[image_index].canonical_text)
                image_index += 1
            else:
                raise ValueError("未知内容分片")
    if image_index != len(image_evidence):
        raise ValueError("图片证据数量多于来源图片")
    modality = "mixed" if text_parts and image_evidence else (
        "image" if image_evidence else "text"
    )
    return "\n".join(summary_parts), "\n".join(text_parts), modality


def build_message_memories(
    messages: Sequence[SourceMessageDraft],
    images_by_message: Sequence[Sequence[GroundedImageEmbedding]],
    text_vectors: Sequence[Sequence[float] | None],
    *,
    model_name: str,
    model_version: str,
) -> list[MemoryDraft]:
    """按消息位置构造证据，图片 URI 不进入可检索摘要。"""
    if len(messages) != len(images_by_message) or len(messages) != len(text_vectors):
        raise ValueError("消息、图片与文本向量数量不一致")

    memories: list[MemoryDraft] = []
    for message, image_evidence, text_vector in zip(
        messages, images_by_message, text_vectors, strict=True
    ):
        summary, original_text, modality = describe_message(message, image_evidence)
        if not summary:
            if text_vector is not None:
                raise ValueError("空消息不应有文本向量")
            continue
        if text_vector is None:
            raise ValueError("非空消息缺少文本向量")

        memories.append(
            MemoryDraft(
                summary=summary,
                original_text=original_text,
                modality=modality,
                embedding=EmbeddingDraft(
                    modality="text",
                    model_name=model_name,
                    model_version=model_version,
                    dimensions=len(text_vector),
                    vector=list(text_vector),
                ),
                image_embeddings=[
                    EmbeddingDraft(
                        modality="image",
                        model_name=model_name,
                        model_version=model_version,
                        dimensions=len(item.vector),
                        vector=list(item.vector),
                    )
                    for item in image_evidence
                ],
                granularity="message",
                source_position=message.position,
            )
        )
    return memories
