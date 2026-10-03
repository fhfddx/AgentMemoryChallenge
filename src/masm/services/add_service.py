"""Add 应用服务与幂等事务编排。

所有权隔离：每次 claim/takeover 都会产生新的所有者标识（fencing token），并持久化在
RequestLedger 上。暂存目录按「用户 + 请求 + 所有者标识」隔离，因此失去所有权的旧处理者
既不能提交、也不能标记失败、更不能清理新所有者的暂存对象。

向量生成：Add 在 claim 之前同步生成文本向量与图片向量，Provider 故障不会遗留 PROCESSING。
"""

import contextlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from masm.config import Settings
from masm.orchestration.add_pipeline import AddPipeline
from masm.providers.embeddings import EmbeddingProvider
from masm.providers.fakes import DeterministicFakeEmbeddingProvider
from masm.providers.multimodal_embeddings import (
    GroundedImageEmbedding,
    GroundedMultimodalEmbeddingProvider,
)
from masm.schemas.api import AddRequest, AddResponse
from masm.schemas.internal import AssetRef, DecodedImage
from masm.services.message_memory import build_message_memories, describe_message
from masm.storage.assets import (
    AssetStore,
    ImageTooLargeError,
    decode_image_data_url,
)
from masm.storage.repositories import (
    ActionApplicationError,
    LedgerStateError,
    MemoryRepository,
    lock_run_lifecycle,
)
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
    images_by_message: Sequence[Sequence[GroundedImageEmbedding]]
    message_vectors: Sequence[Sequence[float] | None]


class AddService:
    """Add 基线服务：保存原始文本、顺序内容分片、图片引用、基础记忆与向量，不调用智能体。"""

    def __init__(
        self,
        repository: MemoryRepository,
        asset_store: AssetStore,
        settings: Settings,
        *,
        embeddings: EmbeddingProvider | None = None,
        pipeline: AddPipeline | None = None,
        clock: Callable[[], datetime] | None = None,
        processing_lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> None:
        self._repo = repository
        self._assets = asset_store
        self._settings = settings
        self._embeddings = embeddings or DeterministicFakeEmbeddingProvider()
        # pipeline 为 None 时保持任务 4 的基线模式（不调用任何智能体）。
        self._pipeline = pipeline
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

        # 智能体编排与向量生成都在 claim 之前完成，Provider 故障不会遗留 PROCESSING。
        # 只包住这一段：媒体解码与校验在上方，400/413/422 语义不受影响。
        try:
            plan = self._pipeline.process(request) if self._pipeline is not None else None
            # 智能体成功时以它的摘要生成向量；降级时回落到基线摘要（含图片基础描述）。
            summary_override = None
            if plan is not None and not plan.degraded and plan.memories:
                summary_override = plan.memories[0].summary
            prepared = self._prepare(request, images, summary_override=summary_override)
        except Exception:
            # 编排或向量生成期间其他处理者可能已用同一 request_id 完成提交；
            # 此时已经成功的请求不得因为本次故障而暴露异常。
            ledger = self._repo.get_ledger(request.user_id, request.request_id)
            if ledger is not None and ledger.status == "COMMITTED":
                return self._replay(request)
            raise

        # 运行级生命周期锁从 claim 一直覆盖到暂存写入、数据库 finalize 与对象发布：
        # Delete 因而不可能在 put 前删掉账本，也不可能在回放/发布之间撤销该运行。
        with self._lifecycle_lock(request.user_id, request.request_id):
            owner_token = self._acquire(request, now)
            if owner_token is None:
                # prepare 期间其他处理者已提交；必须使用锁内新快照回放。
                return self._replay_locked(request)

            asset_refs: list[AssetRef] = []
            try:
                for image in prepared.images:
                    asset_refs.append(
                        self._assets.put(
                            request.user_id, request.request_id, owner_token, image
                        )
                    )
                self._repo.finalize_request(
                    request.user_id,
                    request.request_id,
                    self._build_bundle(request, asset_refs, prepared, plan),
                    owner_token=owner_token,
                )
            except ActionApplicationError:
                # 治理动作不安全：本次请求仍然持有 PROCESSING 账本，降级提交纯基线记忆束。
                return self._degrade(request, asset_refs, prepared, owner_token)
            except LedgerStateError as exc:
                # 已失去所有权：只丢弃本次尝试的暂存，绝不触碰新所有者资源。
                self._discard(request, owner_token)
                raise AddConflictError("请求所有权已失效") from exc
            except Exception:
                self._fail(request, owner_token)
                raise
            else:
                # 数据库已提交后发布本次尝试的暂存对象；失败会抛出 500，重试时由 _replay 修复。
                self._assets.publish(request.user_id, request.request_id, owner_token)

        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
        )

    @contextmanager
    def _lifecycle_lock(self, user_id: str, request_id: str) -> Iterator[None]:
        """持有运行级生命周期锁（独占连接），与删除使用同一把 advisory 锁。"""
        with self._repo.lifecycle_connection() as connection:
            with lock_run_lifecycle(connection, user_id, request_id):
                yield

    def _degrade(
        self,
        request: AddRequest,
        asset_refs: list[AssetRef],
        prepared: _Prepared,
        owner_token: datetime,
    ) -> AddResponse:
        """治理动作应用失败时的降级提交。

        用**同一个 owner token** 原子提交纯基线记忆束：不应用 relation / supersede /
        conflict 等治理动作，账本最终为 COMMITTED，基线记忆与来源可被 Search 检索。
        若期间所有权已被接管，则只丢弃本次尝试的暂存并返回冲突，绝不触碰新 owner 的数据。
        调用方已持有运行级生命周期锁，因此这里不再重复加锁。
        """
        try:
            self._repo.finalize_request(
                request.user_id,
                request.request_id,
                self._build_bundle(request, asset_refs, prepared, None),
                owner_token=owner_token,
            )
        except LedgerStateError as exc:
            self._discard(request, owner_token)
            raise AddConflictError("请求所有权已失效") from exc
        except Exception:
            self._fail(request, owner_token)
            raise
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
        """在生命周期锁内重新读取并回放 COMMITTED 请求。"""
        with self._lifecycle_lock(request.user_id, request.request_id):
            return self._replay_locked(request)

    def _replay_locked(self, request: AddRequest) -> AddResponse:
        """用锁内一致快照验证提交，再发布遗留暂存对象并返回原结果。

        调用方必须持有运行级生命周期锁。必须先同时确认 COMMITTED 账本和对应记忆提交，
        再执行 publish；否则锁外旧账本可能在 Delete 删除数据库后重新发布孤儿对象。
        """
        ledger = self._repo.get_ledger(request.user_id, request.request_id)
        commit = self._repo.get_by_request(request.user_id, request.request_id)
        if ledger is None or ledger.status != "COMMITTED" or commit is None:
            raise AddPreviouslyFailedError("账本与记忆数据不一致")
        self._assets.publish(request.user_id, request.request_id, ledger.owner_token)
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

    def _prepare(
        self,
        request: AddRequest,
        images: Sequence[DecodedImage],
        *,
        summary_override: str | None = None,
    ) -> _Prepared:
        """生成摘要与文本/图片向量（均在 claim 之前完成）。

        summary_override 由智能体编排提供；为 None 时使用基线摘要（含图片基础描述）。
        """
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
        baseline_summary = " ".join(part for part in (text, *descriptions) if part)
        summary = summary_override or baseline_summary
        modality = "mixed" if (text and images) else ("image" if images else "text")

        image_evidence: list[GroundedImageEmbedding] = []
        if images:
            image_bytes = [image.data for image in images]
            if isinstance(self._embeddings, GroundedMultimodalEmbeddingProvider):
                image_evidence = self._embeddings.ground_images(image_bytes)
            else:
                image_vectors = self._embeddings.embed_images(image_bytes)
                if len(image_vectors) != len(images):
                    raise ValueError("图片向量数量与来源图片数量不一致")
                image_evidence = [
                    GroundedImageEmbedding(
                        canonical_text=(
                            f"description: image {image.media_type} {image.width}x{image.height}"
                        ),
                        vector=list(vector),
                    )
                    for image, vector in zip(images, image_vectors, strict=True)
                ]
            if len(image_evidence) != len(images):
                raise ValueError("图片证据数量与来源图片数量不一致")

        images_by_message: list[list[GroundedImageEmbedding]] = []
        source_messages: list[SourceMessageDraft] = []
        image_offset = 0
        for position, message in enumerate(request.messages):
            content = message.content
            image_count = 0 if isinstance(content, str) else sum(
                part.type == "image_url" for part in content
            )
            images_by_message.append(image_evidence[image_offset : image_offset + image_count])
            image_offset += image_count
            source_messages.append(
                SourceMessageDraft(
                    role=message.role,
                    content=(
                        content
                        if isinstance(content, str)
                        else [part.model_dump() for part in content]
                    ),
                    position=position,
                    timestamp=message.timestamp,
                )
            )
        if image_offset != len(image_evidence):
            raise ValueError("图片证据数量与消息图片数量不一致")

        message_summaries = [
            describe_message(message, evidence)[0]
            for message, evidence in zip(source_messages, images_by_message, strict=True)
        ]
        vector_inputs = ([summary] if summary else []) + [
            message_summary for message_summary in message_summaries if message_summary
        ]
        vectors = self._embeddings.embed_texts(vector_inputs) if vector_inputs else []
        if len(vectors) != len(vector_inputs):
            raise ValueError("文本向量数量与记忆摘要数量不一致")
        vector_iter = iter(vectors)
        text_vector = list(next(vector_iter)) if summary else []
        message_vectors = [
            list(next(vector_iter)) if message_summary else None
            for message_summary in message_summaries
        ]

        return _Prepared(
            images=images,
            text=text,
            summary=summary,
            modality=modality,
            text_vector=text_vector,
            image_vectors=[item.vector for item in image_evidence],
            images_by_message=images_by_message,
            message_vectors=message_vectors,
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
        self,
        request: AddRequest,
        asset_refs: list[AssetRef],
        prepared: _Prepared,
        plan: MemoryBundle | None = None,
    ) -> MemoryBundle:
        """构建记忆束：保持内容分片顺序，图片替换为对象引用，并附带向量与已校验动作。"""
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
        # 智能体成功时附加其结构化结论；降级或基线模式只保存基线记忆。
        keywords: Sequence[str] = []
        event_time = None
        time_precision = None
        confidence = None
        if plan is not None and not plan.degraded and plan.memories:
            enriched = plan.memories[0]
            keywords = enriched.keywords
            event_time = enriched.event_time
            time_precision = enriched.time_precision
            confidence = enriched.confidence

        memory = MemoryDraft(
            summary=prepared.summary,
            original_text=prepared.text,
            keywords=keywords,
            modality=prepared.modality,
            event_time=event_time,
            time_precision=time_precision,
            confidence=confidence,
            embedding=embedding,
            image_embeddings=image_embeddings,
        )
        return MemoryBundle(
            session_id=request.session_id,
            request_id=request.request_id,
            messages=messages,
            memories=[
                memory,
                *build_message_memories(
                    messages,
                    prepared.images_by_message,
                    prepared.message_vectors,
                    model_name=self._embeddings.model_name,
                    model_version=self._embeddings.model_version,
                ),
            ],
            assets=asset_refs,
            actions=plan.actions if plan is not None else None,
            degraded=plan.degraded if plan is not None else False,
        )
