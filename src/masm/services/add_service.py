"""Add 应用服务与幂等事务编排。"""

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from masm.config import Settings
from masm.schemas.api import AddRequest, AddResponse
from masm.schemas.internal import AssetRef, DecodedImage
from masm.storage.assets import (
    AssetStore,
    ImageTooLargeError,
    decode_image_data_url,
)
from masm.storage.repositories import LedgerStateError, MemoryRepository
from masm.storage.types import MemoryBundle, MemoryDraft, SourceMessageDraft

# PROCESSING 租约默认时长（秒）；超时后允许安全重新处理或恢复。
DEFAULT_PROCESSING_LEASE_SECONDS = 60.0


def utc_now() -> datetime:
    """默认时钟：返回带时区的 UTC 当前时间。"""
    return datetime.now(UTC)


class AddRequestError(ValueError):
    """Add 请求处理失败。"""

    status_code = 500


class AddConflictError(AddRequestError):
    """请求正在处理中（真实并发）。"""

    status_code = 409


class AddPreviouslyFailedError(AddRequestError):
    """请求此前已失败，不得伪装成功。"""

    status_code = 409


class AddService:
    """Add 基线服务：保存原始文本、顺序内容分片、图片引用与基础记忆，不调用智能体。"""

    def __init__(
        self,
        repository: MemoryRepository,
        asset_store: AssetStore,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        processing_lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> None:
        self._repo = repository
        self._assets = asset_store
        self._settings = settings
        self._clock = clock or utc_now
        self._lease = timedelta(seconds=processing_lease_seconds)

    def add(self, request: AddRequest) -> AddResponse:
        """处理一次 Add；相同 user_id + request_id 只产生一次逻辑写入。"""
        now = self._clock()
        # 媒体解码与总量校验先于幂等 claim，避免 400/413/422 请求遗留 PROCESSING。
        images = self._decode_images(request)

        if not self._acquire(request, now):
            return self._replay(request)

        asset_refs: list[AssetRef] = []
        try:
            for image in images:
                asset_refs.append(self._assets.put(request.user_id, request.request_id, image))
        except Exception:
            self._fail(request)
            raise

        try:
            self._repo.finalize_request(
                request.user_id,
                request.request_id,
                self._build_bundle(request, asset_refs),
            )
        except LedgerStateError as exc:
            # 所有权已被接管：账本由新所有者掌管，丢弃本请求暂存后报冲突。
            with contextlib.suppress(Exception):
                self._assets.discard(request.user_id, request.request_id)
            raise AddConflictError("请求所有权已失效") from exc
        except Exception:
            self._fail(request)
            raise

        # 数据库已提交后发布暂存对象；失败会向上抛出 500，重试时由 _replay 修复。
        self._assets.publish(request.user_id, request.request_id)
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    def _acquire(self, request: AddRequest, now: datetime) -> bool:
        """取得请求所有权。

        返回 True 表示可继续处理；返回 False 表示已有 COMMITTED 结果应回放。
        真实并发的 PROCESSING 抛出 409；租约过期且无已提交数据时允许安全重新处理。
        """
        for _ in range(3):
            ledger = self._repo.get_ledger(request.user_id, request.request_id)
            if ledger is None:
                if self._repo.claim_request(
                    request.user_id, request.request_id, request.session_id, now=now
                ):
                    return True
                continue
            if ledger.status == "COMMITTED":
                return False
            if ledger.status == "FAILED":
                raise AddPreviouslyFailedError("该 request_id 此前处理失败，请更换 request_id")

            # 状态为 PROCESSING：区分真实并发与遗留租约。
            if (now - ledger.updated_at) < self._lease:
                raise AddConflictError("该请求正在处理中")
            if self._repo.has_committed_data(request.user_id, request.request_id):
                self._repo.mark_request(
                    request.user_id,
                    request.request_id,
                    "COMMITTED",
                    now=now,
                    expect="PROCESSING",
                )
                return False
            if self._repo.takeover_request(
                request.user_id,
                request.request_id,
                now=now,
                stale_before=now - self._lease,
            ):
                return True
            raise AddConflictError("该请求正在处理中")
        raise AddConflictError("该请求正在处理中")

    def _fail(self, request: AddRequest) -> None:
        """丢弃本请求的暂存对象，并把仍属于本请求的账本标记为 FAILED。

        清理失败不得阻止账本进入确定状态。
        """
        with contextlib.suppress(Exception):
            self._assets.discard(request.user_id, request.request_id)
        self._mark_failed(request)

    def _mark_failed(self, request: AddRequest) -> None:
        """仅当账本仍为 PROCESSING 时才标记 FAILED，避免覆盖已提交状态。"""
        try:
            self._repo.mark_request(
                request.user_id,
                request.request_id,
                "FAILED",
                now=self._clock(),
                expect="PROCESSING",
            )
        except Exception:
            return

    def _replay(self, request: AddRequest) -> AddResponse:
        """COMMITTED 请求返回先前的逻辑结果，并修复「已提交未发布」的窗口。"""
        self._assets.publish(request.user_id, request.request_id)
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
