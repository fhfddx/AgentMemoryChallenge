"""把入选消息记忆恢复为原始、有序且完整校验的多模态证据。"""

import base64
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from masm.retrieval.reranker import RankedEvidence
from masm.schemas.content import ContentPart, ImageURLPart, TextPart
from masm.storage.assets import SUPPORTED_MEDIA_TYPES, AssetStore
from masm.storage.repositories import MemoryRepository
from masm.storage.types import SourceMessageSnapshot


class EvidenceRenderer:
    """在排名完成后批量恢复 message 证据；任何失败都保留原摘要。"""

    def __init__(
        self,
        repository: MemoryRepository,
        asset_store: AssetStore,
        *,
        max_image_bytes: int,
    ) -> None:
        if max_image_bytes < 1:
            raise ValueError("max_image_bytes 必须为正整数")
        self._repo = repository
        self._assets = asset_store
        self._max_image_bytes = max_image_bytes

    def render(
        self, user_id: str, evidence: Sequence[RankedEvidence]
    ) -> list[RankedEvidence]:
        """恢复可定位的来源消息，同时保持排名、分数与元数据不变。"""
        locations = list(
            dict.fromkeys(
                (item.request_id, item.source_position)
                for item in evidence
                if item.granularity == "message"
                and item.request_id
                and item.source_position is not None
            )
        )
        if not locations:
            return list(evidence)
        snapshots = self._repo.source_messages_for_positions(user_id, locations)
        rendered: list[RankedEvidence] = []
        for item in evidence:
            snapshot = (
                snapshots.get((item.request_id, item.source_position))
                if item.source_position is not None
                else None
            )
            if item.granularity != "message" or snapshot is None:
                rendered.append(item)
                continue
            try:
                content = self._render_snapshot(snapshot)
            except (KeyError, OSError, TypeError, ValueError):
                content = None
            rendered.append(item if content is None else replace(item, content=content))
        return rendered

    def _render_snapshot(
        self, snapshot: SourceMessageSnapshot
    ) -> str | list[ContentPart] | None:
        content = snapshot.content
        if isinstance(content, str):
            return content if content.strip() else None
        if not isinstance(content, list) or not content:
            return None

        parts: list[ContentPart] = []
        for raw in content:
            if not isinstance(raw, dict):
                raise TypeError("来源内容分片必须为对象")
            part_type = raw.get("type")
            if part_type == "text":
                text = raw.get("text")
                if not isinstance(text, str):
                    raise TypeError("文本分片缺少文本")
                parts.append(TextPart(text=text))
                continue
            if part_type != "image_url":
                raise ValueError("未知来源内容分片")
            object_uri = _object_uri(raw)
            asset = snapshot.assets[object_uri]
            if asset.media_type not in SUPPORTED_MEDIA_TYPES:
                raise ValueError("来源图片类型不受支持")
            payload = self._assets.read_object(
                asset.object_uri,
                max_bytes=self._max_image_bytes,
                expected_size=asset.decoded_size,
                expected_hash=asset.content_hash,
            )
            encoded = base64.b64encode(payload).decode("ascii")
            parts.append(
                ImageURLPart(
                    image_url={"url": f"data:{asset.media_type};base64,{encoded}"}
                )
            )
        return parts or None


def _object_uri(raw: dict[str, Any]) -> str:
    image_url = raw.get("image_url")
    if not isinstance(image_url, dict):
        raise TypeError("图片分片缺少 image_url")
    value = image_url.get("url")
    if not isinstance(value, str) or not value:
        raise TypeError("图片分片缺少对象地址")
    return value
