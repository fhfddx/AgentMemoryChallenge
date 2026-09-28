"""存储层领域类型。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
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
    """一条结构化记忆草稿。"""

    summary: str
    original_text: str
    keywords: Sequence[str] = ()
    modality: str = "text"
    event_time: datetime | None = None
    time_precision: str | None = None
    confidence: float | None = None
    embedding: EmbeddingDraft | None = None


@dataclass(frozen=True)
class MemoryBundle:
    """一次 Add 请求要持久化的记忆束。"""

    session_id: str
    request_id: str
    messages: Sequence[SourceMessageDraft] = ()
    memories: Sequence[MemoryDraft] = ()
    assets: Sequence[AssetRef] = ()


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
    """检索返回的记忆候选。"""

    memory_id: UUID
    user_id: str
    content: str
    score: float


@dataclass(frozen=True)
class AddCommit:
    """一次 Add 提交的结果。"""

    request_id: str
    user_id: str
    session_id: str
    memory_ids: Sequence[UUID] = ()
