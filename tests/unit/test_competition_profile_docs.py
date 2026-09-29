"""比赛实验配置必须映射到实际可运行档位。"""

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _config(name: str) -> dict:
    path = _ROOT / "experiments" / "configs" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_b0_uses_official_baseline_models_and_remains_blocked_before_real_run() -> None:
    config = _config("b0.yaml")

    assert config["deployment_profile"] == "official-baseline"
    assert config["models"] == {
        "embedding": "text-embedding-v4",
        "llm": "gpt-4o-mini",
        "reranker": "none",
        "provider_revision": "record-before-run",
    }


def test_masm_uses_official_masm_models_and_remains_blocked_before_real_run() -> None:
    config = _config("masm.yaml")

    assert config["deployment_profile"] == "official-masm"
    assert config["models"] == {
        "embedding": "text-embedding-v4",
        "llm": "gpt-4o-mini",
        "reranker": "lexical-overlap",
        "provider_revision": "record-before-run",
    }
    assert config["prompts"] == {
        "perception": "perception_v1",
        "temporal": "temporal_v1",
        "curator": "curator_v1",
    }
