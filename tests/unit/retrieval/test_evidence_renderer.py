"""Original text and multimodal evidence rendering tests."""

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from masm.retrieval.evidence_renderer import EvidenceRenderer
from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ImageURLPart, TextPart
from masm.schemas.internal import DecodedImage
from masm.storage.assets import AssetStore
from masm.storage.types import SourceMessageSnapshot, StoredAssetSnapshot


class _SnapshotRepository:
    def __init__(self, snapshots) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, tuple[tuple[str, int], ...]]] = []

    def source_messages_for_positions(self, user_id: str, positions):
        keys = tuple(positions)
        self.calls.append((user_id, keys))
        return {key: self.snapshots[key] for key in keys if key in self.snapshots}


def _evidence(
    content: str = "ranked summary",
    *,
    granularity: str = "message",
    request_id: str = "run-1",
    source_position: int | None = 0,
) -> RankedEvidence:
    return RankedEvidence(
        memory_id=uuid4(),
        user_id="user-1",
        content=content,
        score=0.75,
        rank=2,
        granularity=granularity,
        request_id=request_id,
        source_position=source_position,
        metadata={"base_score": 0.1},
    )


def _published(store: AssetStore, payload: bytes) -> StoredAssetSnapshot:
    image = DecodedImage(
        media_type="image/png",
        data=payload,
        decoded_size=len(payload),
        content_hash=hashlib.sha256(payload).hexdigest(),
        width=1,
        height=1,
    )
    token = datetime(2026, 1, 1, tzinfo=UTC)
    ref = store.put("user-1", "run-1", token, image)
    store.publish("user-1", "run-1", token)
    return StoredAssetSnapshot(
        object_uri=ref.object_uri,
        media_type=ref.media_type,
        decoded_size=ref.decoded_size,
        content_hash=ref.content_hash,
    )


def test_plain_text_source_replaces_summary_without_changing_rank_metadata(tmp_path: Path) -> None:
    original = _evidence()
    repository = _SnapshotRepository({("run-1", 0): SourceMessageSnapshot("exact original")})
    renderer = EvidenceRenderer(repository, AssetStore(tmp_path / "assets"), max_image_bytes=1024)

    rendered = renderer.render("user-1", [original])

    assert rendered[0].content == "exact original"
    assert rendered[0].memory_id == original.memory_id
    assert rendered[0].score == original.score
    assert rendered[0].rank == original.rank
    assert rendered[0].metadata == original.metadata


def test_ordered_text_image_text_source_is_rehydrated(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    payload = b"original-image-bytes"
    asset = _published(store, payload)
    snapshot = SourceMessageSnapshot(
        content=[
            {"type": "text", "text": "before"},
            {"type": "image_url", "image_url": {"url": asset.object_uri}},
            {"type": "text", "text": "after"},
        ],
        assets={asset.object_uri: asset},
    )
    renderer = EvidenceRenderer(
        _SnapshotRepository({("run-1", 0): snapshot}), store, max_image_bytes=1024
    )

    rendered = renderer.render("user-1", [_evidence()])[0]

    assert isinstance(rendered.content, list)
    assert [type(part) for part in rendered.content] == [TextPart, ImageURLPart, TextPart]
    assert rendered.content[0].text == "before"
    image_url = rendered.content[1].image_url.url
    assert base64.b64decode(image_url.split(",", 1)[1]) == payload
    assert rendered.content[2].text == "after"


def test_one_invalid_part_keeps_the_entire_ranked_summary(tmp_path: Path) -> None:
    snapshot = SourceMessageSnapshot(
        content=[
            {"type": "text", "text": "private original text"},
            {"type": "image_url", "image_url": {"url": "missing/object.png"}},
        ],
        assets={},
    )
    original = _evidence("safe summary")
    renderer = EvidenceRenderer(
        _SnapshotRepository({("run-1", 0): snapshot}),
        AssetStore(tmp_path / "assets"),
        max_image_bytes=1024,
    )

    rendered = renderer.render("user-1", [original])

    assert rendered[0].content == "safe summary"


def test_context_evidence_is_unchanged_without_source_lookup(tmp_path: Path) -> None:
    repository = _SnapshotRepository({})
    original = _evidence(granularity="context", source_position=None)
    renderer = EvidenceRenderer(repository, AssetStore(tmp_path / "assets"), max_image_bytes=1024)

    rendered = renderer.render("user-1", [original])

    assert rendered == [original]
    assert repository.calls == []


def test_missing_or_wrong_user_snapshot_keeps_summary(tmp_path: Path) -> None:
    repository = _SnapshotRepository({})
    original = _evidence("scoped summary")
    renderer = EvidenceRenderer(repository, AssetStore(tmp_path / "assets"), max_image_bytes=1024)

    rendered = renderer.render("other-user", [original])

    assert rendered[0].content == "scoped summary"
    assert repository.calls == [("other-user", (("run-1", 0),))]
