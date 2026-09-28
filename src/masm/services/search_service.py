"""Search 应用服务（基线混合检索，只返回记忆证据）。"""

from masm.retrieval.baseline import BaselineRetriever, ParsedQuery
from masm.schemas.api import MemoryEvidence, SearchRequest, SearchResponse
from masm.storage.assets import decode_image_data_url


class SearchService:
    """Search 基线服务：不做最终答案生成，只返回命中证据。"""

    def __init__(self, retriever: BaselineRetriever, *, max_image_bytes: int) -> None:
        self._retriever = retriever
        self._max_image_bytes = max_image_bytes

    def search(self, request: SearchRequest) -> SearchResponse:
        """执行查询分析、混合召回与证据打包。"""
        parsed = self.analyze(request)
        candidates = self._retriever.retrieve(request.user_id, parsed, request.top_k)
        return SearchResponse(
            data=[
                MemoryEvidence(
                    id=str(candidate.memory_id),
                    content=candidate.content,
                    score=candidate.score,
                )
                for candidate in candidates
            ]
        )

    def analyze(self, request: SearchRequest) -> ParsedQuery:
        """规则化查询分析：纯文本查询或保持顺序的多模态查询。"""
        query = request.query
        if isinstance(query, str):
            return ParsedQuery(text_queries=(query,), intent="fact")

        texts: list[str] = []
        images: list[bytes] = []
        for part in query:
            if part.type == "text":
                texts.append(part.text)
            else:
                image = decode_image_data_url(part.image_url.url, self._max_image_bytes)
                images.append(image.data)
        return ParsedQuery(
            text_queries=tuple(texts),
            visual_queries=tuple(images),
            intent="visual" if images else "fact",
        )
