"""Mem-Gallery 小型子集适配器。

规范化输入与 ATM-Bench 适配器相同，从而让两个公开基准共享完全一致的评测路径。
"""

from pathlib import Path

from experiments.adapters.atm_bench import load_dataset as _load_normalized_dataset
from experiments.models import BenchmarkDataset


def load_dataset(path: Path) -> BenchmarkDataset:
    return _load_normalized_dataset(path)
