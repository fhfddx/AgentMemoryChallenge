"""Add 应用服务与幂等事务编排。"""

from masm.config import Settings
from masm.schemas.api import AddRequest, AddResponse
from masm.schemas.internal import AssetRef, DecodedImage
from masm.storage.assets import (
    AssetStore,
    ImageTooLargeError,
    decode_image_data_url,
)
from masm.storage.repositories import MemoryRepository
from masm.storage.types import MemoryBundle, MemoryDraft, SourceMessageDraft


class AddRequestError(ValueError):
    """Add 请求处理失败。"""

    status_code = 500


class AddConflictError(AddRequestError):
    """请求正在处理中（并发冲突）。"""

    status_code = 409


class AddPreviouslyFailedError(AddRequestError):
    """请求此前已失败，不得伪装成功。"""

    status_code = 409


class AddService:
    """Add 基线服务：保存原始文本、顺序内容分片、图片引用与基础记忆，不调用智能体。"""

    def __init__(
        self, repository: MemoryRepository, asset_store: AssetStore, settings: Settings
    ) -> None:
        self._repo = repository
        self._assets = asset_store
        self._settings = settings

    def add(self, request: AddRequest) -> AddResponse:
        """处理一次 Add；相同 user_id + request_id 只产生一次逻辑写入。"""
        status = self._repo.get_ledger_status(request.user_id, request.request_id)
        if status == "COMMITTED":
            return self._replay(request)
        if status == "FAILED":
            raise AddPreviouslyFailedError("该 request_id 此前处理失败，请更换 request_id")
        if status == "PROCESSING":
            raise AddConflictError("该请求正在处理中")

        images = self._decode_images(request)

        if not self._repo.claim_request(request.user_id, request.request_id, request.session_id):
            status = self._repo.get_ledger_status(request.user_id, request.request_id)
            if status == "COMMITTED":
                return self._replay(request)
            raise AddConflictError("该请求正在处理中")

        asset_refs: list[AssetRef] = []
        try:
            for image in images:
                asset_refs.append(self._assets.put(request.user_id, request.request_id, image))
        except Exception:
            self._repo.mark_request(request.user_id, request.request_id, "FAILED")
            raise

        try:
            self._repo.add_bundle(request.user_id, self._build_bundle(request, asset_refs))
        except Exception:
            self._repo.mark_request(request.user_id, request.request_id, "FAILED")
            raise

        self._repo.mark_request(request.user_id, request.request_id, "COMMITTED")
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    def _replay(self, request: AddRequest) -> AddResponse:
        """COMMITTED 请求返回先前的逻辑结果。"""
        commit = self._repo.get_by_request(request.user_id, request.request_id)
        if commit is None:
            raise AddPreviouslyFailedError("账本与记忆数据不一致")
        return AddResponse(
            success=True,
            request_id=commit.request_id,
            user_id=commit.user_id,
            session_id=commit.session_id,
        )

    def _decode_images(self, request: AddRequest) -> list[DecodedImage]:
        """解码并校验全部图片，同时累加单次 Add 图片总量。"""
        images: list[DecodedImage] = []
        total = 0
        for message in request.messages:
            content = message.content
            if not isinstance(content, list):
                continue
            for part in content:
                if part.type == "image_url":
                    image = decode_image_data_url(
                        part.image_url.url, self._settings.max_image_bytes
                    )
                    total += image.decoded_size
                    if total > self._settings.max_add_image_bytes:
                        raise ImageTooLargeError("单次 Add 图片总量超过上限")
                    images.append(image)
        return images

    def _build_bundle(self, request: AddRequest, asset_refs: list[AssetRef]) -> MemoryBundle:
        """构建记忆束：保持内容分片顺序，图片替换为对象引用。"""
        messages: list[SourceMessageDraft] = []
        text_parts: list[str] = []
        has_image = False
        image_iter = iter(asset_refs)

        for position, message in enumerate(request.messages):
            content = message.content
            if isinstance(content, str):
                serialized: str | list[dict] = content
                text_parts.append(content)
            else:
                parts: list[dict] = []
                for part in content:
                    if part.type == "text":
                        parts.append({"type": "text", "text": part.text})
                        text_parts.append(part.text)
                    else:
                        has_image = True
                        ref = next(image_iter)
                        parts.append({"type": "image_url", "image_url": {"url": ref.object_uri}})
                serialized = parts
            messages.append(
                SourceMessageDraft(
                    role=message.role,
                    content=serialized,
                    position=position,
                    timestamp=message.timestamp,
                )
            )

        text = "\n".join(part for part in text_parts if part)
        modality = "mixed" if (text and has_image) else ("image" if has_image else "text")
        memory = MemoryDraft(summary=text, original_text=text, keywords=[], modality=modality)
        return MemoryBundle(
            session_id=request.session_id,
            request_id=request.request_id,
            messages=messages,
            memories=[memory],
            assets=asset_refs,
        )
