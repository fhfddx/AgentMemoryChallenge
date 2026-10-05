"""本地评估在真实 API/数据库路径上必须清理精确测试运行。"""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
from scripts.evaluate_synthetic_recall import EvaluationCase
from scripts.metered_holdout import (
    build_holdout_cases,
    load_external_media_cases,
    meter_app_stages,
    run_app_holdout,
    summarize_stage_latency,
)
from sqlalchemy import text


def test_run_app_holdout_cleans_exact_add_run(
    client: TestClient, database, tmp_path: Path,
) -> None:
    """漏写清理清单或残留 Add 行时失败。"""
    tag = uuid4().hex[:12]
    marker = f"holdout-{tag}-evidence"
    case = EvaluationCase(
        "preference", (({"role": "user", "content": f"Notebook {marker}."},),),
        marker, (marker,),
    )
    manifest = tmp_path / "cleanup.tsv"
    report = run_app_holdout(
        client.app, [case], version="v1.0", run_tag=tag,
        api_key="test-key", cleanup_output=manifest,
    )

    run_id = f"synthetic-holdout-{tag}-v1.0-preference-0"
    assert report["categories"]["preference"]["recall_at_10"] == 1.0
    assert manifest.read_text(encoding="utf-8").strip().endswith(run_id)
    with database.engine.connect() as connection:
        for table in ("request_ledger", "source_messages", "memories", "assets"):
            count = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE request_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
            assert count == 0, table


def test_external_style_abstention_exposes_baseline_false_recall_and_cleans_distractor(
    client: TestClient, database, tmp_path: Path,
) -> None:
    """记录基线对无关历史的误召回；评分不得掩盖，且精确清理 Add。"""
    tag = uuid4().hex[:12]
    case = EvaluationCase(
        "abstention", (({"role": "user", "content": "I like blue notebooks."},),),
        "What is my passport number?", (), expect_empty=True,
    )
    report = run_app_holdout(
        client.app, [case], version="v1.0", run_tag=tag,
        api_key="test-key", cleanup_output=tmp_path / "cleanup.tsv",
    )

    assert report["categories"]["abstention"]["recall_at_10"] == 0.0
    assert report["categories"]["abstention"]["empty_result_rate"] == 0.0
    run_id = f"synthetic-holdout-{tag}-v1.0-abstention-0"
    with database.engine.connect() as connection:
        for table in ("request_ledger", "source_messages", "memories", "assets"):
            assert connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE request_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one() == 0, table


def test_media_diagnostic_cleans_image_assets_and_rows(
    client: TestClient, database, asset_store, tmp_path: Path,
) -> None:
    """Fake DB 路径证明媒体诊断的图片对象和四表行精确删除。"""
    tag = uuid4().hex[:12]
    cases = build_holdout_cases(tag, case_set="media-v1")
    report = run_app_holdout(
        client.app, cases, version="v1.0", run_tag=tag,
        api_key="test-key", cleanup_output=tmp_path / "cleanup.tsv",
    )

    assert set(report["categories"]) == {"text_image", "image_only"}
    assert len((tmp_path / "cleanup.tsv").read_text(encoding="utf-8").splitlines()) == 2
    with database.engine.connect() as connection:
        for category in ("text_image", "image_only"):
            run_id = f"synthetic-holdout-{tag}-v1.0-{category}-0"
            for table in ("request_ledger", "source_messages", "memories", "assets"):
                assert connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE request_id = :run_id"),
                    {"run_id": run_id},
                ).scalar_one() == 0, (category, table)
    assert not any(path.is_file() for path in asset_store.base_dir.rglob("*"))


def test_external_media_holdout_loads_and_cleans_image_assets_and_rows(
    client: TestClient, database, asset_store, tmp_path: Path,
) -> None:
    """外部图片清单经 Fake API 路径运行后不得残留图片或四表行。"""
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), (20, 70, 180)).save(image_path, format="PNG")
    source = tmp_path / "external-media.json"
    source.write_text(json.dumps({
        "source_name": "independent-cc0-media-set",
        "selection_rule": "first eligible image in source order",
        "cases": [{
            "id": "image-001", "category": "external_media_one",
            "source_url": "https://example.org/media/one", "license": "CC0-1.0",
            "image_file": image_path.name,
            "image_sha256": sha256(image_path.read_bytes()).hexdigest(),
            "query": "Which animal is visible?", "expected_markers": ["animal-marker"],
        }],
    }), encoding="utf-8")
    cases, audit = load_external_media_cases(
        source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
    )
    tag = uuid4().hex[:12]

    report = run_app_holdout(
        client.app, cases, version="v1.0", run_tag=tag,
        api_key="test-key", cleanup_output=tmp_path / "cleanup.tsv",
    )

    assert set(report["categories"]) == {"external_media_one"}
    assert audit["source_item_ids"] == ["image-001"]
    run_id = f"synthetic-holdout-{tag}-v1.0-external_media_one-0"
    with database.engine.connect() as connection:
        for table in ("request_ledger", "source_messages", "memories", "assets"):
            assert connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE request_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one() == 0, table
    assert not any(path.is_file() for path in asset_store.base_dir.rglob("*"))


def test_stage_meter_observes_real_local_add_and_cleanup(
    database, asset_store, settings, embeddings, tmp_path: Path,
) -> None:
    """真实 API/测试库路径应产生各阶段记录，清理后仍无测试运行残留。"""
    from masm.api.app import create_app
    from masm.config import RuntimeProfile
    from masm.providers.fakes import FakeStructuredLLM
    from masm.schemas.agents import (
        ActionKind,
        CuratorAction,
        CuratorDecision,
        PerceptionResult,
        TemporalRelationResult,
    )

    tag = uuid4().hex[:12]
    marker = f"diagnostic-{tag}"
    fake_llm = FakeStructuredLLM([
        PerceptionResult(keywords=(marker,), language="en"),
        TemporalRelationResult(),
        CuratorDecision(actions=(CuratorAction(
            kind=ActionKind.CREATE, confidence=0.8, evidence="observed evidence",
        ),)),
    ])
    official_settings = replace(
        settings, runtime_profile=RuntimeProfile.OFFICIAL_MASM,
        llm_base_url="https://example.invalid", llm_api_key="test-only",
        embedding_base_url="https://example.invalid", embedding_api_key="test-only",
    )
    app = create_app(
        official_settings, database=database, asset_store=asset_store,
        embeddings=embeddings, llm=fake_llm,
    )
    case = EvaluationCase(
        "diagnostic", (({"role": "user", "content": f"Notebook {marker}."},),),
        marker, (marker,),
    )
    with meter_app_stages(app) as stages:
        report = run_app_holdout(
            app, [case], version="v1.1", run_tag=tag, api_key="test-key",
            cleanup_output=tmp_path / "cleanup.tsv",
        )

    assert report["categories"]["diagnostic"]["recall_at_10"] == 1.0
    summary = summarize_stage_latency(stages)
    for stage in ("perception", "recall", "temporal", "embedding_prepare",
                  "commit", "search"):
        assert summary[stage]["count"] == 1
        assert summary[stage]["failures"] == 0
    assert "curator" not in summary
    run_id = f"synthetic-holdout-{tag}-v1.1-diagnostic-0"
    with database.engine.connect() as connection:
        for table in ("request_ledger", "source_messages", "memories", "assets"):
            count = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE request_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
            assert count == 0, table
