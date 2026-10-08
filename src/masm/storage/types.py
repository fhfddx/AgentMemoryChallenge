"""存储层领域类型。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from masm.schemas.internal import AssetRef


@dataclass(frozen=True)
class SourceMessageDraft:
    """一条保持原始顺序的原始消息草稿。"""

    role: str
    content: Any  # 有序内容分片（纯文本或内容数组，JSON 可序列化）
    position: int = 0
    timestamp: int | None = None


@dataclass(frozen=True)
class EmbeddingDraft:
    """一条带模型版本的向量记录草稿。"""

    modality: str  # text / image
    model_name: str
    model_version: str
    dimensions: int
    vector: Sequence[float]


@dataclass(frozen=True)
class MemoryDraft:
    """一条结构化记忆草稿。

    embedding 是文本向量；image_embeddings 是该记忆关联图片的图片向量。
    """

    summary: str
    original_text: str
    keywords: Sequence[str] = ()
    modality: str = "text"
    event_time: datetime | None = None
    time_precision: str | None = None
    confidence: float | None = None
    embedding: EmbeddingDraft | None = None
    image_embeddings: Sequence[EmbeddingDraft] = ()
    granularity: Literal["context", "message"] = "context"
    source_position: int | None = None


@dataclass(frozen=True)
class RelationDraft:
    """待写入的同用户关系边；源端固定为本次新建的那条记忆。"""

    target_id: UUID
    relation_type: str
    confidence: float | None = None
    evidence_ref: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ValidatedActions:
    """通过确定性校验的记忆管理动作。

    所有数据库治理变更（关系边、重复标记、替代、冲突组）都只能由该对象驱动，
    它不包含任何可以改写原始证据的字段。
    """

    relations: Sequence[RelationDraft] = ()
    duplicate_of: UUID | None = None
    supersedes: UUID | None = None
    conflict_group_id: UUID | None = None
    conflict_targets: Sequence[UUID] = ()


@dataclass(frozen=True)
class MemoryBundle:
    """一次 Add 请求要持久化的记忆束。"""

    session_id: str
    request_id: str
    messages: Sequence[SourceMessageDraft] = ()
    memories: Sequence[MemoryDraft] = ()
    assets: Sequence[AssetRef] = ()
    actions: ValidatedActions | None = None
    degraded: bool = False


@dataclass(frozen=True)
class LedgerState:
    """幂等账本状态快照。

    owner_token 是持久化在 RequestLedger 上的所有者标识（每次 claim/takeover 都会产生新值），
    同时充当租约时间戳与 fencing token：旧所有者持有旧 token，无法再提交、标记失败或清理
    新所有者的资源。
    """

    status: str
    owner_token: datetime


@dataclass(frozen=True)
class MemoryCandidate:
    """检索返回的记忆候选。

    supersedes / status / conflict_group_id 是治理字段：召回通道与检索器都必须完整保留，
    确定性动作校验据此判断替代链与冲突组延续。
    """

    memory_id: UUID
    user_id: str
    content: str
    score: float
    supersedes: UUID | None = None
    status: str = "active"
    conflict_group_id: UUID | None = None
    duplicate_of: UUID | None = None
    granularity: Literal["context", "message"] = "context"
    request_id: str = ""
    source_position: int | None = None
    retrieval_signals: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DeletedRun:
    """一次运行删除的**数据库阶段**结果。

    只描述可回滚的数据库工作：``object_uris`` 是本运行独占（无任何用户/运行再引用）、
    已登记为 PENDING 删除意图的对象地址；``shared_object_uris`` 是仍被引用的对象地址；
    ``staging_uris`` 是该运行各所有者需要清理的暂存目录意图地址。
    物理删除结果由 :class:`ObjectDeletionOutcome` 单独报告。
    """

    request_id: str
    memories: int = 0
    assets: int = 0
    sources: int = 0
    relations: int = 0
    conflicts: int = 0
    object_uris: Sequence[str] = ()
    shared_object_uris: Sequence[str] = ()
    staging_uris: Sequence[str] = ()


@dataclass(frozen=True)
class ObjectDeletionOutcome:
    """一次物理删除重试的结果。

    ``failed`` 同时包含对象删除失败与暂存清理失败：两者都必须保持 PENDING 以便重试，
    因此报告的 ``complete`` 必须为 False。
    """

    deleted: int = 0
    missing: int = 0
    failed: Sequence[tuple[str, str]] = ()
    still_referenced: Sequence[str] = ()
    staging_cleaned: int = 0
    staging_failed: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class AddCommit:
    """一次 Add 提交的结果。"""

    request_id: str
    user_id: str
    session_id: str
    memory_ids: Sequence[UUID] = ()
