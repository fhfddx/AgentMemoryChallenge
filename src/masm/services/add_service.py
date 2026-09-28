"""Add 应用服务与幂等事务编排。

所有权隔离：每次 claim/takeover 都会产生新的所有者标识（fencing token），并持久化在
RequestLedger 上。暂存目录按「用户 + 请求 + 所有者标识」隔离，因此失去所有权的旧处理者
既不能提交、也不能标记失败、更不能清理新所有者的暂存对象。

向量生成：Add 在 claim 之前同步生成文本向量与图片向量，Provider 故障不会遗留 PROCESSING。
"""

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from masm.config import Settings
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.schemas.api import AddRequest, AddResponse
from masm.schemas.internal import AssetRef, DecodedImage
from masm.storage.assets import (
    AssetStore,
    ImageTooLargeError,
    decode_image_data_url,
)
from masm.storage.repositories import LedgerStateError, MemoryRepository
from masm.storage.types import (
    EmbeddingDraft,
    MemoryBundle,
    MemoryDraft,
    SourceMessageDraft,
)

# PROCESSING 租约默认时长（秒）；超时后允许安全重新处理或恢复。
DEFAULT_PROCESSING_LEASE_SECONDS = 60.0


def utc_now() -> datetime:
    """默认时钟：返回带时区的 UTC 当前时间。"""
    return datetime.now(UTC)


class AddRequestError(ValueError):
    """Add 请求处理失败。"""

    status_code = 500


class AddConflictError(AddRequestError):
    """请求正在处理中（真实并发或所有权已失效）。"""

    status_code = 409


class AddPreviouslyFailedError(AddRequestError):
    """请求此前已失败，不得伪装成功。"""

    status_code = 409


@dataclass(frozen=True)
class _Prepared:
    """claim 之前就绪的内容：图片、文本摘要与全部向量。"""

    images: Sequence[DecodedImage]
    text: str
    summary: str
    modality: str
    text_vector: Sequence[float]
    image_vectors: Sequence[Sequence[float]]


class AddService:
    """Add 基线服务：保存原始文本、顺序内容分片、图片引用、基础记忆与向量，不调用智能体。"""

    def __init__(
        self,
        repository: MemoryRepository,
        asset_store: AssetStore,
        settings: Settings,
        *,
        embeddings: EmbeddingProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        processing_lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> None:
        self._repo = repository
        self._assets = asset_store
        self._settings = settings
        self._embeddings = embeddings or DeterministicFakeEmbeddingProvider()
        self._clock = clock or utc_now
        self._lease = timedelta(seconds=processing_lease_seconds)

    def add(self, request: AddRequest) -> AddResponse:
        """处理一次 Add；相同 user_id + request_id 只产生一次逻辑写入。"""
        now = self._clock()
        # 媒体解码与总量校验先于一切幂等与向量工作，保持既有的 400/413/422 语义。
        images = self._decode_images(request)

        # 已提交请求直接回放：绝不调用 Embedding Provider，也不重复写入。
        ledger = self._repo.get_ledger(request.user_id, request.request_id)
        if ledger is not None and ledger.status == "COMMITTED":
            return self._replay(request)

        # 摘要与向量仍在 claim 之前生成，Provider 故障不会遗留 PROCESSING。
        prepared = self._prepare(request, images)

        owner_token = self._acquire(request, now)
        if owner_token is None:
            # 初次检查之后、prepare 期间其他处理者完成了提交：回放原结果。
            return self._replay(request)

        asset_refs: list[AssetRef] = []
        try:
            for image in prepared.images:
                asset_refs.append(
                    self._assets.put(request.user_id, request.request_id, owner_token, image)
                )
        except Exception:
            self._fail(request, owner_token)
            raise

        try:
            self._repo.finalize_request(
                request.user_id,
                request.request_id,
                self._build_bundle(request, asset_refs, prepared),
                owner_token=owner_token,
            )
        except LedgerStateError as exc:
            # 已失去所有权：只丢弃本次尝试的暂存，绝不触碰新所有者资源。
            self._discard(request, owner_token)
            raise AddConflictError("请求所有权已失效") from exc
        except Exception:
            self._fail(request, owner_token)
            raise

        # 数据库已提交后发布本次尝试的暂存对象；失败会抛出 500，重试时由 _replay 修复。
        self._assets.publish(request.user_id, request.request_id, owner_token)
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    def _acquire(self, request: AddRequest, now: datetime) -> datetime | None:
        """取得请求所有权，返回本次所有者标识；返回 None 表示应回放已有结果。"""
        for _ in range(3):
            ledger = self._repo.get_ledger(request.user_id, request.request_id)
            if ledger is None:
                token = self._repo.claim_request(
                    request.user_id, request.request_id, request.session_id, now=now
                )
                if token is not None:
                    return token
                continue
            if ledger.status == "COMMITTED":
                return None
            if ledger.status == "FAILED":
                raise AddPreviouslyFailedError("该 request_id 此前处理失败，请更换 request_id")

            # 状态为 PROCESSING：区分真实并发与遗留租约。
            if (now - ledger.owner_token) < self._lease:
                raise AddConflictError("该请求正在处理中")
            if self._repo.has_committed_data(request.user_id, request.request_id):
                self._repo.mark_request(
                    request.user_id,
                    request.request_id,
                    "COMMITTED",
                    owner_token=ledger.owner_token,
                )
                return None
            token = self._repo.takeover_request(
                request.user_id,
                request.request_id,
                now=now,
                stale_before=now - self._lease,
            )
            if token is not None:
                return token
            raise AddConflictError("该请求正在处理中")
        raise AddConflictError("该请求正在处理中")

    def _fail(self, request: AddRequest, owner_token: datetime) -> None:
        """丢弃本次尝试的暂存，并把仍由本次尝试持有的账本标记为 FAILED。"""
        self._discard(request, owner_token)
        with contextlib.suppress(Exception):
            self._repo.mark_request(
                request.user_id,
                request.request_id,
                "FAILED",
                owner_token=owner_token,
            )

    def _discard(self, request: AddRequest, owner_token: datetime) -> None:
        """丢弃本次尝试的暂存目录；失败不得覆盖原始异常。"""
        with contextlib.suppress(Exception):
            self._assets.discard(request.user_id, request.request_id, owner_token)

    def _replay(self, request: AddRequest) -> AddResponse:
        """COMMITTED 请求返回先前的逻辑结果，并发布该所有者遗留的暂存对象。"""
        ledger = self._repo.get_ledger(request.user_id, request.request_id)
        if ledger is not None:
            self._assets.publish(request.user_id, request.request_id, ledger.owner_token)
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

    def _prepare(self, request: AddRequest, images: Sequence[DecodedImage]) -> _Prepared:
        """生成基础摘要与文本/图片向量（均在 claim 之前完成）。"""
        text_parts: list[str] = []
        for message in request.messages:
            content = message.content
            if isinstance(content, str):
                text_parts.append(content)
            else:
                text_parts.extend(part.text for part in content if part.type == "text")
        text = "\n".join(part for part in text_parts if part).strip()

        descriptions = [
            f"image {image.media_type} {image.width}x{image.height}" for image in images
        ]
        summary = " ".join(part for part in (text, *descriptions) if part)
        modality = "mixed" if (text and images) else ("image" if images else "text")

        text_vector: list[float] = []
        if summary:
            text_vector = list(self._embeddings.embed_texts([summary])[0])
        image_vectors: list[list[float]] = []
        if images:
            image_vectors = [
                list(vector)
                for vector in self._embeddings.embed_images([image.data for image in images])
            ]

        return _Prepared(
            images=images,
            text=text,
            summary=summary,
            modality=modality,
            text_vector=text_vector,
            image_vectors=image_vectors,
        )

    def _embedding_draft(self, modality: str, vector: Sequence[float]) -> EmbeddingDraft:
        return EmbeddingDraft(
            modality=modality,
            model_name=self._embeddings.model_name,
            model_version=self._embeddings.model_version,
            dimensions=len(vector),
            vector=list(vector),
        )

    def _build_bundle(
        self, request: AddRequest, asset_refs: list[AssetRef], prepared: _Prepared
    ) -> MemoryBundle:
        """构建记忆束：保持内容分片顺序，图片替换为对象引用，并附带文本与图片向量。"""
        messages: list[SourceMessageDraft] = []
        image_iter = iter(asset_refs)

        for position, message in enumerate(request.messages):
            content = message.content
            if isinstance(content, str):
                serialized: str | list[dict] = content
            else:
                parts: list[dict] = []
                for part in content:
                    if part.type == "text":
                        parts.append({"type": "text", "text": part.text})
                    else:
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

        embedding = (
            self._embedding_draft("text", prepared.text_vector)
            if prepared.text_vector
            else None
        )
        image_embeddings = [
            self._embedding_draft("image", vector) for vector in prepared.image_vectors
        ]
        memory = MemoryDraft(
            summary=prepared.summary,
            original_text=prepared.text,
            keywords=[],
            modality=prepared.modality,
            embedding=embedding,
            image_embeddings=image_embeddings,
        )
        return MemoryBundle(
            session_id=request.session_id,
            request_id=request.request_id,
            messages=messages,
            memories=[memory],
            assets=asset_refs,
        )
