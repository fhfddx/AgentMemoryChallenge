"""ATM-Bench 子集适配器。

当前接受仓库固定样例使用的规范化 JSON；下载原基准后应先离线转换，在线运行器不会读取
Repository 或任何内部答案。
"""

from __future__ import annotations

import json
from pathlib import Path

from experiments.models import BenchmarkDataset, BenchmarkItem, BenchmarkQuery


def load_dataset(path: Path) -> BenchmarkDataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = tuple(
        BenchmarkItem(
            id=str(item["id"]),
            user_id=str(item["user_id"]),
            session_id=str(item["session_id"]),
            messages=tuple(item["messages"]),
            match_token=str(item["match_token"]),
        )
        for item in raw["items"]
    )
    queries = tuple(
        BenchmarkQuery(
            id=str(query["id"]),
            user_id=str(query["user_id"]),
            query=query["query"],
            relevant_item_ids=tuple(str(value) for value in query["relevant_item_ids"]),
        )
        for query in raw["queries"]
    )
    return BenchmarkDataset(version=str(raw["version"]), items=items, queries=queries)
