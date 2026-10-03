"""消息级记忆保持来源位置与图文证据顺序。"""

from masm.providers.multimodal_embeddings import GroundedImageEmbedding
from masm.storage.types import SourceMessageDraft


def test_one_add_yields_context_then_ordered_messages() -> None:
    from masm.services.message_memory import build_message_memories

    messages = [
        SourceMessageDraft(role="user", content="Alice likes tea", position=0),
        SourceMessageDraft(role="assistant", content="She visits Kyoto", position=1),
    ]

    memories = build_message_memories(
        messages,
        images_by_message=[[], []],
        text_vectors=[[1.0, 0.0], [0.0, 1.0]],
        model_name="fake",
        model_version="v1",
    )

    assert [(item.granularity, item.source_position) for item in memories] == [
        ("message", 0),
        ("message", 1),
    ]
    assert [item.original_text for item in memories] == ["Alice likes tea", "She visits Kyoto"]
    assert [list(item.embedding.vector) for item in memories if item.embedding] == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]


def test_empty_and_image_only_messages() -> None:
    from masm.services.message_memory import build_message_memories

    messages = [
        SourceMessageDraft(role="user", content="   ", position=0),
        SourceMessageDraft(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": "objects/photo.png"}}],
            position=1,
        ),
    ]

    memories = build_message_memories(
        messages,
        images_by_message=[
            [],
            [GroundedImageEmbedding("description: red sign\nocr: STOP", [0.1, 0.9])],
        ],
        text_vectors=[None, [0.2, 0.8]],
        model_name="fake",
        model_version="v1",
    )

    assert len(memories) == 1
    assert (memories[0].granularity, memories[0].source_position) == ("message", 1)
    assert memories[0].summary == "description: red sign\nocr: STOP"
    assert memories[0].original_text == ""
    assert memories[0].modality == "image"
    assert list(memories[0].embedding.vector) == [0.2, 0.8]
    assert [list(item.vector) for item in memories[0].image_embeddings] == [[0.1, 0.9]]


def test_interleaved_parts_keep_image_alignment() -> None:
    from masm.services.message_memory import build_message_memories

    messages = [
        SourceMessageDraft(
            role="user",
            position=3,
            content=[
                {"type": "text", "text": "before"},
                {"type": "image_url", "image_url": {"url": "objects/red.png"}},
                {"type": "text", "text": "after"},
                {"type": "image_url", "image_url": {"url": "objects/blue.png"}},
            ],
        )
    ]

    memories = build_message_memories(
        messages,
        images_by_message=[
            [
                GroundedImageEmbedding("description: red", [1.0, 0.0]),
                GroundedImageEmbedding("description: blue", [0.0, 1.0]),
            ]
        ],
        text_vectors=[[0.5, 0.5]],
        model_name="fake",
        model_version="v1",
    )

    assert memories[0].summary == "before\ndescription: red\nafter\ndescription: blue"
    assert memories[0].original_text == "before\nafter"
    assert memories[0].source_position == 3
    assert [list(item.vector) for item in memories[0].image_embeddings] == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]
