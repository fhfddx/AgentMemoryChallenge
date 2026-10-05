"""本地留出集的真实用量计量与脱敏边界。"""

import base64
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import scripts.metered_holdout as metered_holdout
from fastapi import FastAPI
from PIL import Image
from scripts.evaluate_synthetic_recall import EvaluationCase


def test_metered_client_records_numeric_usage_without_payload_or_key() -> None:
    """若计量器遗漏真实 usage 或保存正文/密钥，这个测试应失败。"""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "usage": {
            "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        meter = metered_holdout.MeteredClient(upstream, provider="llm", model="test-model")
        meter.post(
            "https://example.invalid/chat/completions",
            json={"messages": [{"content": "private-message-marker"}]},
            headers={"Authorization": "Bearer private-key-marker"},
        )

    assert len(meter.records) == 1
    assert meter.records[0].usage == {
        "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
    }
    serialized = json.dumps(metered_holdout.summarize_usage(meter.records), sort_keys=True)
    assert "private-message-marker" not in serialized
    assert "private-key-marker" not in serialized
    assert "private-message-marker" not in repr(meter.records)
    assert "private-key-marker" not in repr(meter.records)


def test_metered_client_counts_image_bearing_http_attempts_without_image_payload() -> None:
    """图片重试按实际 HTTP 尝试计数，绝不保留 Data URI 或计作图片专属费用。"""
    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "private-error-marker"})
        return httpx.Response(200, json={"usage": {
            "prompt_tokens": 30, "completion_tokens": 4,
        }})

    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "private-text-marker"},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,private-image-marker",
        }},
    ]}]}
    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        meter = metered_holdout.MeteredClient(upstream, provider="llm", model="test-model")
        meter.post("https://example.invalid/chat/completions", json=body)
        meter.post("https://example.invalid/chat/completions", json=body)

    summary = metered_holdout.summarize_usage(meter.records)
    assert summary["llm_calls"] == 2
    assert summary["llm_image_attempts"] == 2
    assert summary["llm_image_blocks"] == 2
    assert summary["image_usage_missing"] == 1
    assert summary["estimated_cost"] == "unavailable"
    for marker in ("private-image-marker", "private-text-marker", "private-error-marker"):
        assert marker not in repr(meter.records)
        assert marker not in json.dumps(summary)


def test_image_counter_ignores_text_and_embedding_inputs() -> None:
    """图片计数只由 LLM messages 的 image_url 块决定。"""
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {
            "prompt_tokens": 2, "completion_tokens": 1,
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        llm = metered_holdout.MeteredClient(upstream, provider="llm", model="test-model")
        embedding = metered_holdout.MeteredClient(
            upstream, provider="embedding", model="embed-model",
        )
        llm.post("https://example.invalid/chat", json={"messages": [
            {"role": "user", "content": "image_url is only text"},
        ]})
        embedding.post("https://example.invalid/embed", json={
            "input": ["image_url is only text"],
        })
    summary = metered_holdout.summarize_usage([*llm.records, *embedding.records])
    assert summary["llm_image_attempts"] == 0
    assert summary["llm_image_blocks"] == 0
    assert summary["image_usage_missing"] == 0


def test_metered_client_hard_attempt_limit_prevents_another_http_request() -> None:
    """重试也计入预算；到达上限后必须在网络请求前失败。"""
    sent = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(503, json={"error": "private-error-marker"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        meter = metered_holdout.MeteredClient(
            upstream, provider="llm", model="test-model", attempt_limit=2,
        )
        meter.post("https://example.invalid/first", json={})
        meter.post("https://example.invalid/second", json={})
        with pytest.raises(RuntimeError, match="尝试次数达到上限") as failure:
            meter.post("https://example.invalid/third", json={
                "messages": [{"content": "private-text-marker"}],
            })

    assert sent == 2
    assert len(meter.records) == 2
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("exhausted_stage", ["add", "search"])
def test_holdout_aborts_when_provider_limit_is_swallowed_by_fallback(
    exhausted_stage: str, tmp_path: Path,
) -> None:
    """Add/Search 即使吞掉上限异常，评估边界也必须终止而非产出报告。"""

    def provider_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1}})

    with httpx.Client(transport=httpx.MockTransport(provider_response)) as upstream:
        meter = metered_holdout.MeteredClient(
            upstream, provider="llm", model="test-model", attempt_limit=1,
        )
        meter.post("https://example.invalid/first", json={})
        app = FastAPI()

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/add")
        def add() -> dict[str, bool]:
            if exhausted_stage == "add":
                try:
                    meter.post("https://example.invalid/add", json={})
                except RuntimeError:
                    pass
            return {"success": True}

        @app.post("/search")
        def search() -> dict[str, list[object]]:
            if exhausted_stage == "search":
                try:
                    meter.post("https://example.invalid/search", json={})
                except RuntimeError:
                    pass
            return {"data": []}

        additions = (
            (({"role": "user", "content": "private-source"},),)
            if exhausted_stage == "add"
            else ()
        )
        case = EvaluationCase("limit", additions, "Question?", (), expect_empty=True)
        with pytest.raises(RuntimeError, match="尝试次数达到上限"):
            metered_holdout.run_app_holdout(
                app, [case], version="v1.1", run_tag="fixed", api_key="local-key",
                cleanup_output=tmp_path / f"{exhausted_stage}-cleanup.tsv",
                cleanup_run=lambda _user, _run: True,
                attempt_meters=(meter,),
            )


def test_cli_rejects_invalid_attempt_limit_before_database_or_provider(tmp_path: Path) -> None:
    """命令行上限必须在连接测试库和创建应用前校验。"""
    with pytest.raises(ValueError, match="尝试上限必须为正整数"):
        metered_holdout.main([
            "--source-root", str(tmp_path), "--version", "v1.1",
            "--asset-dir", str(tmp_path / ".superpowers" / "assets"),
            "--output", str(tmp_path / "report.json"),
            "--cleanup-output", str(tmp_path / "cleanup.tsv"),
            "--max-llm-attempts", "0",
        ])
    assert not (tmp_path / "report.json").exists()


def test_usage_summary_counts_retries_and_never_guesses_missing_usage() -> None:
    """缺失 usage 的一次请求不能被当作零费用。"""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/first"):
            return httpx.Response(503, json={"error": "private-error-marker"})
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        meter = metered_holdout.MeteredClient(upstream, provider="embedding", model="embed-model")
        for suffix in ("first", "second"):
            meter.post(
                f"https://example.invalid/{suffix}",
                json={"input": ["secret-one", "secret-two"]},
            )

    summary = metered_holdout.summarize_usage(meter.records, prices_per_million={
        "llm_input_usd": Decimal("0.15"),
        "llm_output_usd": Decimal("0.60"),
        "embedding_input_cny": Decimal("0.02"),
    })
    assert summary["embedding_calls"] == 2
    assert summary["embedding_inputs"] == 4
    assert summary["http_failures"] == 1
    assert summary["usage_missing"] == 2
    assert summary["estimated_cost"] == "unavailable"
    assert "private-error-marker" not in json.dumps(summary)


def test_usage_estimate_requires_explicit_prices_and_complete_token_fields() -> None:
    """费率缺失或 LLM 输入/输出 token 不完整时不得给出费用。"""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/llm"):
            return httpx.Response(200, json={"choices": [], "usage": {
                "prompt_tokens": 1_000_000, "completion_tokens": 500_000,
            }})
        return httpx.Response(200, json={"data": [], "usage": {
            "prompt_tokens": 2_000_000,
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream:
        llm = metered_holdout.MeteredClient(upstream, provider="llm", model="llm-model")
        embedding = metered_holdout.MeteredClient(
            upstream, provider="embedding", model="embed-model",
        )
        llm.post("https://example.invalid/llm", json={})
        embedding.post("https://example.invalid/embedding", json={"input": ["one"]})

    records = [*llm.records, *embedding.records]
    assert metered_holdout.summarize_usage(records)["estimated_cost"] == "unavailable"
    priced = metered_holdout.summarize_usage(records, prices_per_million={
        "llm_input_usd": Decimal("0.15"),
        "llm_output_usd": Decimal("0.60"),
        "embedding_input_cny": Decimal("0.02"),
    })
    assert priced["estimated_cost"] == {
        "llm_usd": "0.45", "embedding_cny": "0.04", "combined": "unavailable",
    }


def test_price_estimate_keeps_openai_usd_and_aliyun_cny_separate() -> None:
    """不同币种不能直接相加成一个 USD 总额。"""
    records = [
        metered_holdout.UsageRecord(
            "llm", "gpt-4o-mini", 200, 0,
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        ),
        metered_holdout.UsageRecord(
            "embedding", "text-embedding-v4", 200, 1,
            {"prompt_tokens": 1_000_000},
        ),
    ]
    summary = metered_holdout.summarize_usage(records, prices_per_million={
        "llm_input_usd": Decimal("0.15"),
        "llm_output_usd": Decimal("0.60"),
        "embedding_input_cny": Decimal("0.50"),
    })
    assert summary["estimated_cost"] == {
        "llm_usd": "0.75", "embedding_cny": "0.5", "combined": "unavailable",
    }


def test_holdout_cases_are_small_and_queries_do_not_reveal_answer_markers() -> None:
    """留出集不应退化为把答案标记原样复制进查询。"""
    cases = metered_holdout.build_holdout_cases("fixed")
    assert len(cases) == 6
    assert sum(len(case.additions) for case in cases) <= 8
    assert {case.category for case in cases} == {
        "preference", "conditional_plan", "cross_session", "multi_fact",
        "distractor", "abstention",
    }
    for case in cases:
        for marker in case.expected_markers:
            assert marker not in str(case.query)


def test_primary_case_set_remains_the_default() -> None:
    """新增模板选择后，省略参数仍须得到原始留出集。"""
    assert metered_holdout.build_holdout_cases("fixed") == (
        metered_holdout.build_holdout_cases("fixed", case_set="primary")
    )


def test_alternate_case_set_changes_semantics_but_preserves_shape() -> None:
    """替代模板不能复用原文，也不能改变类别和 Add 规模。"""
    primary = metered_holdout.build_holdout_cases("fixed", case_set="primary")
    alternate = metered_holdout.build_holdout_cases("fixed", case_set="alternate-v1")

    assert [case.category for case in alternate] == [
        "preference", "conditional_plan", "cross_session", "multi_fact",
        "distractor", "abstention",
    ]
    assert [len(case.additions) for case in alternate] == [1, 1, 2, 1, 1, 0]
    for original, replacement in zip(primary, alternate, strict=True):
        assert replacement.query != original.query
        if replacement.additions:
            assert replacement.additions != original.additions
        for marker in replacement.expected_markers:
            assert marker not in replacement.query


def test_media_case_set_is_two_bounded_image_paths() -> None:
    """媒体诊断只选现有固定 PNG 的图文 Add 与纯图 Add/图片 Search。"""
    cases = metered_holdout.build_holdout_cases("fixed", case_set="media-v1")
    assert [case.category for case in cases] == ["text_image", "image_only"]
    assert [len(case.additions) for case in cases] == [1, 1]
    assert isinstance(cases[0].query, str)
    assert isinstance(cases[1].query, list)
    assert cases[1].expect_nonempty is True
    assert sum(len(session) for case in cases for session in case.additions) == 2


def test_unknown_case_set_is_rejected() -> None:
    """拼错模板名时必须在发起评估前明确失败。"""
    with pytest.raises(ValueError, match="未知留出集"):
        metered_holdout.build_holdout_cases("fixed", case_set="unknown")


def test_runtime_profile_selection_distinguishes_b0_from_deployed_masm() -> None:
    """代码版本与运行档位是独立维度，不得默认把 B0 当线上 v1.0。"""
    assert metered_holdout.resolve_runtime_profile("v1.0", None) == "official-baseline"
    assert metered_holdout.resolve_runtime_profile(
        "v1.0", "official-masm",
    ) == "official-masm"
    assert metered_holdout.resolve_runtime_profile("v1.1", None) == "official-masm"
    with pytest.raises(ValueError, match="v1.1 必须使用 official-masm"):
        metered_holdout.resolve_runtime_profile("v1.1", "official-baseline")


def test_external_cases_require_pinned_hash_and_keep_only_audit_metadata(tmp_path: Path) -> None:
    """未锁定的输入或把样本正文写进报告都应被测试发现。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "independent-public-set",
        "source_url": "https://example.org/dataset",
        "selection_rule": "first eligible item in source order",
        "cases": [{
            "id": "q-001", "category": "external_recall",
            "sessions": [[{"role": "user", "content": "The address is private-marker."}]],
            "query": "Which address was recorded?",
            "expected_markers": ["private-marker"],
        }],
    }), encoding="utf-8")
    pinned = sha256(source.read_bytes()).hexdigest()

    cases, audit = metered_holdout.load_external_cases(source, expected_sha256=pinned)

    assert len(cases) == 1
    assert cases[0] == EvaluationCase(
        "external_recall", (({"role": "user", "content": "The address is private-marker."},),),
        "Which address was recorded?", ("private-marker",),
    )
    assert audit == {
        "source_name": "independent-public-set",
        "source_url": "https://example.org/dataset",
        "selection_rule": "first eligible item in source order",
        "source_file_sha256": pinned,
        "source_item_ids": ["q-001"],
    }
    assert "private-marker" not in json.dumps(audit)


def test_external_media_cases_pin_images_and_keep_payload_out_of_audit(
    tmp_path: Path,
) -> None:
    """独立图片必须逐张锁定，且正文、答案和 base64 都不能进入报告。"""
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), (20, 70, 180)).save(image_path, format="PNG")
    image_digest = sha256(image_path.read_bytes()).hexdigest()
    source = tmp_path / "external-media.json"
    source.write_text(json.dumps({
        "source_name": "independent-cc0-media-set",
        "selection_rule": "first eligible image in source order",
        "cases": [{
            "id": "image-001", "category": "external_media_one",
            "source_url": "https://example.org/media/one",
            "license": "CC0-1.0", "image_file": image_path.name,
            "image_sha256": image_digest,
            "query": "Which animal is visible?",
            "expected_markers": ["private-animal-marker"],
        }],
    }), encoding="utf-8")
    pinned = sha256(source.read_bytes()).hexdigest()

    cases, audit = metered_holdout.load_external_media_cases(
        source, expected_sha256=pinned,
    )

    assert len(cases) == 1
    assert cases[0].category == "external_media_one"
    message = cases[0].additions[0][0]
    assert message["role"] == "user"
    assert message["content"][0] == {
        "type": "text",
        "text": "Independent CC0 reference image; remember only the visible content.",
    }
    image_url = message["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    assert image_url.endswith(encoded_image)
    assert cases[0].query == "Which animal is visible?"
    assert cases[0].expected_markers == ("private-animal-marker",)
    assert audit == {
        "source_name": "independent-cc0-media-set",
        "selection_rule": "first eligible image in source order",
        "source_file_sha256": pinned,
        "source_item_ids": ["image-001"],
        "media": [{
            "id": "image-001",
            "source_url": "https://example.org/media/one",
            "license": "CC0-1.0",
            "image_sha256": image_digest,
        }],
    }
    serialized_audit = json.dumps(audit)
    assert encoded_image not in serialized_audit
    assert "private-animal-marker" not in serialized_audit
    assert str(image_path) not in serialized_audit


@pytest.mark.parametrize(("image_file", "image_sha256"), [
    ("../outside.png", None),
    ("sample.png", "0" * 64),
])
def test_external_media_cases_reject_traversal_or_changed_image(
    tmp_path: Path, image_file: str, image_sha256: str | None,
) -> None:
    """越界路径或内容漂移必须在启动应用和付费请求前失败。"""
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), (20, 70, 180)).save(image_path, format="PNG")
    source = tmp_path / "external-media.json"
    source.write_text(json.dumps({
        "source_name": "independent-cc0-media-set",
        "selection_rule": "first eligible image in source order",
        "cases": [{
            "id": "image-001", "category": "external_media_one",
            "source_url": "https://example.org/media/one",
            "license": "CC0-1.0", "image_file": image_file,
            "image_sha256": image_sha256 or sha256(image_path.read_bytes()).hexdigest(),
            "query": "Which animal is visible?",
            "expected_markers": ["private-animal-marker"],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="外部媒体样本无效"):
        metered_holdout.load_external_media_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_media_cases_reject_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录内合法文件名不得借符号链接读取清单目录外的图片。"""
    manifest_dir = tmp_path / "manifest"
    outside_dir = tmp_path / "outside"
    manifest_dir.mkdir()
    outside_dir.mkdir()
    outside_image = outside_dir / "outside.png"
    Image.new("RGB", (8, 8), (20, 70, 180)).save(outside_image, format="PNG")
    image_link = manifest_dir / "sample.png"
    try:
        image_link.symlink_to(outside_image)
    except OSError:
        Image.new("RGB", (8, 8), (20, 70, 180)).save(image_link, format="PNG")
        original_is_symlink = Path.is_symlink

        def simulated_is_symlink(candidate: Path) -> bool:
            return candidate == image_link or original_is_symlink(candidate)

        monkeypatch.setattr(Path, "is_symlink", simulated_is_symlink)
    source = manifest_dir / "external-media.json"
    source.write_text(json.dumps({
        "source_name": "independent-cc0-media-set",
        "selection_rule": "first eligible image in source order",
        "cases": [{
            "id": "image-001", "category": "external_media_one",
            "source_url": "https://example.org/media/one",
            "license": "CC0-1.0", "image_file": image_link.name,
            "image_sha256": sha256(outside_image.read_bytes()).hexdigest(),
            "query": "Which animal is visible?",
            "expected_markers": ["private-animal-marker"],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="外部媒体样本无效"):
        metered_holdout.load_external_media_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_allow_explicit_abstention_with_distractor_history(tmp_path: Path) -> None:
    """独立来源拒答案例不能借非空答案标记伪装成正例。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "independent-public-set",
        "source_url": "https://example.org/dataset",
        "selection_rule": "first unanswered question with unrelated history",
        "cases": [{
            "id": "q-absent", "category": "abstention",
            "sessions": [[{"role": "user", "content": "I like blue notebooks."}]],
            "query": "What is my passport number?", "expected_markers": [],
            "expect_empty": True,
        }],
    }), encoding="utf-8")

    cases, audit = metered_holdout.load_external_cases(
        source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
    )

    assert cases == [EvaluationCase(
        "abstention", (({"role": "user", "content": "I like blue notebooks."},),),
        "What is my passport number?", (), expect_empty=True,
    )]
    assert audit["source_item_ids"] == ["q-absent"]


@pytest.mark.parametrize(("markers", "expect_empty"), [
    ([], False), (["blue"], True), ([], "true"),
])
def test_external_cases_reject_ambiguous_abstention_contract(
    tmp_path: Path, markers: list[str], expect_empty: object,
) -> None:
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [{
            "id": "q-absent", "category": "abstention",
            "sessions": [[{"role": "user", "content": "I like blue notebooks."}]],
            "query": "What is my passport number?", "expected_markers": markers,
            "expect_empty": expect_empty,
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_reject_changed_file_before_evaluation(tmp_path: Path) -> None:
    """两版使用的外部文件发生改变时，必须在任何 Add 前拒绝。"""
    source = tmp_path / "external.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 不匹配"):
        metered_holdout.load_external_cases(source, expected_sha256="0" * 64)


@pytest.mark.parametrize("bad_case", [
    {"id": "q-001", "category": "bad/slug", "sessions": [[{
        "role": "user", "content": "private-marker"}]], "query": "Question?",
     "expected_markers": ["private-marker"]},
    {"id": "q-001", "category": "recall", "sessions": [[{
        "role": "user", "content": "private-marker", "has_answer": True}]],
     "query": "Question?", "expected_markers": ["private-marker"]},
    {"id": "q-001", "category": "recall", "sessions": [[{
        "role": "user", "content": "private-marker"}]],
     "query": "What is private-marker?", "expected_markers": ["private-marker"]},
])
def test_external_cases_reject_unsafe_or_leaky_input(
    tmp_path: Path, bad_case: dict[str, object],
) -> None:
    """无效 ID、标签泄漏及额外来源字段不得进入 API 请求。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [bad_case],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_reject_unhashable_role_with_controlled_error(tmp_path: Path) -> None:
    """异常 JSON 类型不得以 TypeError 越过输入校验。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [{
            "id": "q-001", "category": "recall", "sessions": [[{
                "role": ["user"], "content": "private-marker"}]],
            "query": "Question?", "expected_markers": ["private-marker"],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_reject_oversized_total_before_evaluation(tmp_path: Path) -> None:
    """单条消息合法但总正文超限时不能产生过量付费请求。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [{
            "id": f"q-{index}", "category": f"recall_{index}",
            "sessions": [[
                {"role": "user", "content": "private-marker " + "x" * 1900},
                {"role": "assistant", "content": "y" * 1900},
            ]],
            "query": "Question?", "expected_markers": ["private-marker"],
        } for index in range(6)],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="超过上限"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_reject_malformed_source_url_without_echoing_it(tmp_path: Path) -> None:
    """来源 URL 无效时只返回通用校验错误，不回显可能含凭据的字段。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://[private-token",
        "selection_rule": "first", "cases": [{
            "id": "q-001", "category": "recall", "sessions": [[{
                "role": "user", "content": "private-marker"}]],
            "query": "Question?", "expected_markers": ["private-marker"],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效") as failure:
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )
    assert "private-token" not in str(failure.value)


def test_external_cases_reject_whitespace_only_answer_marker(tmp_path: Path) -> None:
    """空白标记不得把任意含空格的检索结果算作命中。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [{
            "id": "q-001", "category": "recall", "sessions": [[{
                "role": "user", "content": "The answer is A."}]],
            "query": "Which item?", "expected_markers": [" "],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(("field", "value"), [
    ("source_name", "private-marker"),
    ("source_url", "https://example.org/private-marker"),
    ("selection_rule", "first private-marker question"),
    ("id", "private-marker"),
    ("category", "private-marker"),
])
def test_external_cases_reject_answer_in_report_metadata(
    tmp_path: Path, field: str, value: str,
) -> None:
    """会被写入报告的来源字段不能包含答案正文。"""
    source = tmp_path / "external.json"
    case = {
        "id": "q-001", "category": "recall", "sessions": [[{
            "role": "user", "content": "The answer is private-marker."}]],
        "query": "Which item?", "expected_markers": ["private-marker"],
    }
    payload = {
        "source_name": "public-set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [case],
    }
    if field in case:
        case[field] = value
    else:
        payload[field] = value
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效") as failure:
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )
    assert "private-marker" not in str(failure.value)


@pytest.mark.parametrize("source_url", [
    "https://localhost/dataset", "https://127.0.0.1/dataset",
])
def test_external_cases_require_public_source_url(tmp_path: Path, source_url: str) -> None:
    """本机地址不能被误记为可核验的公开来源。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public-set", "source_url": source_url,
        "selection_rule": "first", "cases": [{
            "id": "q-001", "category": "recall", "sessions": [[{
                "role": "user", "content": "private-marker"}]],
            "query": "Question?", "expected_markers": ["private-marker"],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


def test_external_cases_reject_lone_surrogate_before_paid_run(tmp_path: Path) -> None:
    """报告无法编码的来源元数据必须在启动应用前失败。"""
    source = tmp_path / "external.json"
    source.write_text(json.dumps({
        "source_name": "public\ud800set", "source_url": "https://example.org/dataset",
        "selection_rule": "first", "cases": [{
            "id": "q-001", "category": "recall", "sessions": [[{
                "role": "user", "content": "private-marker"}]],
            "query": "Question?", "expected_markers": ["private-marker"],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="外部样本无效"):
        metered_holdout.load_external_cases(
            source, expected_sha256=sha256(source.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("case_set", ["external", "external-media"])
def test_external_modes_validate_file_before_starting_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case_set: str,
) -> None:
    """CLI 必须读取外部文件并在初始化应用前拒绝无效输入。"""
    source = tmp_path / "external.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "result.json"
    cleanup = tmp_path / "cleanup.tsv"
    branch_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv(
        "MASM_TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/masm_test",
    )
    with pytest.raises(ValueError, match="外部.*样本无效"):
        metered_holdout.main([
            "--source-root", str(branch_root), "--version", "v1.1",
            "--asset-dir", str(branch_root / ".superpowers" / "external-case-test"),
            "--output", str(output), "--cleanup-output", str(cleanup),
            "--case-set", case_set, "--external-cases", str(source),
            "--external-cases-sha256", sha256(source.read_bytes()).hexdigest(),
        ])
    assert not output.exists()
    assert not cleanup.exists()


def test_evaluate_client_scores_evidence_without_emitting_payload() -> None:
    """若评估器漏掉检索证据或把正文写入报告，这个测试应失败。"""
    marker = "private-answer-marker"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/add":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"data": [{"content": marker}]})

    case = EvaluationCase(
        "preference", (({"role": "user", "content": marker},),),
        "Which item?", (marker,),
    )
    cleanup: list[tuple[str, str]] = []
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        report = metered_holdout.evaluate_client(
            client, [case], version="v1.1", run_tag="fixed", api_key="secret-key",
            register_cleanup=cleanup.append,
        )
    assert report["categories"]["preference"]["recall_at_10"] == 1.0
    assert cleanup == [(
        "synthetic-holdout-fixed-v1.1-preference",
        "synthetic-holdout-fixed-v1.1-preference-0",
    )]
    assert marker not in json.dumps(report)
    assert "secret-key" not in json.dumps(report)


def test_evaluate_client_registers_cleanup_before_failed_add() -> None:
    """Add 返回错误时必须保留精确清理标识。"""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(503, json={"detail": "private-error"})

    case = EvaluationCase(
        "preference", (({"role": "user", "content": "private-source"},),),
        "Question?", ("answer",),
    )
    cleanup: list[tuple[str, str]] = []
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError, match="Add HTTP 503"):
            metered_holdout.evaluate_client(
                client, [case], version="v1.0", run_tag="fixed", api_key="secret-key",
                register_cleanup=cleanup.append,
            )
    assert cleanup == [(
        "synthetic-holdout-fixed-v1.0-preference",
        "synthetic-holdout-fixed-v1.0-preference-0",
    )]


def test_run_app_holdout_writes_manifest_and_cleans_on_failed_add(tmp_path: Path) -> None:
    """API 失败时仍持久化精确清单并调用限定范围的清理。"""
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/add")
    def add() -> tuple[dict[str, str], int]:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="private-error")

    case = EvaluationCase(
        "preference", (({"role": "user", "content": "private-source"},),),
        "Question?", ("answer",),
    )
    cleaned: list[tuple[str, str]] = []
    manifest = tmp_path / "cleanup.tsv"
    with pytest.raises(RuntimeError, match="Add HTTP 503"):
        metered_holdout.run_app_holdout(
            app, [case], version="v1.0", run_tag="fixed", api_key="local-key",
            cleanup_output=manifest,
            cleanup_run=lambda user, run: cleaned.append((user, run)) or True,
        )
    expected = (
        "synthetic-holdout-fixed-v1.0-preference",
        "synthetic-holdout-fixed-v1.0-preference-0",
    )
    assert cleaned == [expected]
    assert manifest.read_text(encoding="utf-8").strip() == "\t".join(expected)
    assert "private-source" not in manifest.read_text(encoding="utf-8")


def test_cleanup_attempts_every_registered_run_even_if_one_fails(tmp_path: Path) -> None:
    """一个运行清理失败时不能跳过后续运行。"""
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/add")
    def add() -> dict[str, bool]:
        return {"success": True}

    @app.post("/search")
    def search() -> dict[str, list[object]]:
        return {"data": []}

    case = EvaluationCase(
        "cross_session", (
            ({"role": "user", "content": "first"},),
            ({"role": "user", "content": "second"},),
        ), "Question?", ("answer",),
    )
    cleaned: list[str] = []

    def cleanup(_user: str, run: str) -> bool:
        cleaned.append(run)
        return run.endswith("-1")

    with pytest.raises(RuntimeError, match="清理未完成"):
        metered_holdout.run_app_holdout(
            app, [case], version="v1.1", run_tag="fixed", api_key="local-key",
            cleanup_output=tmp_path / "cleanup.tsv", cleanup_run=cleanup,
        )
    assert cleaned == [
        "synthetic-holdout-fixed-v1.1-cross_session-0",
        "synthetic-holdout-fixed-v1.1-cross_session-1",
    ]


def test_meter_runtime_instruments_both_providers_and_restores_clients() -> None:
    """遗漏 Embedding 或离开评估后残留包装客户端都应失败。"""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    llm = SimpleNamespace(_client=None, model="llm-model", records=[])
    text_embeddings = SimpleNamespace(_client=None, model_name="embed-model")
    runtime = SimpleNamespace(
        llm=llm,
        embeddings=SimpleNamespace(_text_embeddings=text_embeddings),
    )
    with metered_holdout.meter_runtime(
        runtime, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
    ) as meters:
        llm._client.post("https://example.invalid/chat/completions", json={})
        text_embeddings._client.post(
            "https://example.invalid/embeddings", json={"input": ["a", "b"]},
        )
        summary = metered_holdout.summarize_usage(
            [record for meter in meters for record in meter.records]
        )
    assert summary["llm_calls"] == 1
    assert summary["embedding_calls"] == 1
    assert summary["embedding_inputs"] == 2
    assert llm._client is None
    assert text_embeddings._client is None


def test_add_stage_meter_records_only_fixed_names_and_restores_methods() -> None:
    """遗漏某段 Add 耗时、把正文写进记录或退出后残留包装时应失败。"""
    secret = "private-source-marker"
    perception = SimpleNamespace(extract=lambda value: value.upper())
    temporal = SimpleNamespace(analyze=lambda value: value)
    curator = SimpleNamespace(propose=lambda value: value)
    repo = SimpleNamespace(finalize_request=lambda value: value)
    pipeline = SimpleNamespace(
        _perception=perception,
        _recall=lambda value: value,
        _temporal=temporal,
        _curator=curator,
    )
    service = SimpleNamespace(_pipeline=pipeline, _prepare=lambda value: value, _repo=repo)
    search = SimpleNamespace(search=lambda value: value)
    app = SimpleNamespace(state=SimpleNamespace(add_service=service, search_service=search))
    original_extract = perception.extract

    with metered_holdout.meter_app_stages(app) as records:
        assert perception.extract(secret) == secret.upper()
        assert pipeline._recall(secret) == secret
        assert temporal.analyze(secret) == secret
        assert curator.propose(secret) == secret
        assert service._prepare(secret) == secret
        assert repo.finalize_request(secret) == secret
        assert search.search(secret) == secret

    assert [row.stage for row in records] == [
        "perception", "recall", "temporal", "curator", "embedding_prepare",
        "commit", "search",
    ]
    assert all(row.latency_ms >= 0 and row.succeeded for row in records)
    assert secret not in repr(records)
    assert perception.extract is original_extract
    assert metered_holdout.summarize_stage_latency(records)["perception"]["count"] == 1


def test_stage_meter_records_failure_without_exception_message() -> None:
    """失败阶段也有耗时，但不得保存异常消息。"""
    def fail(_value: str) -> None:
        raise ValueError("private-error-marker")

    pipeline = SimpleNamespace(
        _perception=SimpleNamespace(extract=fail),
        _recall=lambda value: value,
        _temporal=SimpleNamespace(analyze=lambda value: value),
        _curator=SimpleNamespace(propose=lambda value: value),
    )
    service = SimpleNamespace(
        _pipeline=pipeline, _prepare=lambda value: value,
        _repo=SimpleNamespace(finalize_request=lambda value: value),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=service, search_service=SimpleNamespace(search=lambda value: value),
    ))
    with metered_holdout.meter_app_stages(app) as records:
        with pytest.raises(ValueError, match="private-error-marker"):
            pipeline._perception.extract("private-source-marker")
    assert len(records) == 1
    assert records[0].stage == "perception"
    assert records[0].succeeded is False
    assert "private" not in repr(records)
    assert metered_holdout.summarize_stage_latency(records)["perception"]["failures"] == 1


def test_baseline_without_add_pipeline_still_times_search() -> None:
    """v1.0 没有 Add 智能体时不能连 Search 阶段也跳过。"""
    search = SimpleNamespace(search=lambda value: value)
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=SimpleNamespace(_pipeline=None), search_service=search,
    ))
    with metered_holdout.meter_app_stages(app) as records:
        assert search.search("private-query-marker") == "private-query-marker"
    assert [row.stage for row in records] == ["search"]
    assert "private-query-marker" not in repr(records)


@pytest.mark.parametrize((
    "first_response", "expected_kind", "expected_status", "expected_http_failures",
), [
    ("http_503", "http_status", 503, 1),
    ("http_302", "http_status", 302, 1),
    ("timeout", "timeout", None, 1),
    ("invalid_json", "response_validation", None, 0),
])
def test_meter_runtime_classifies_recovered_provider_attempt_without_leaking_content(
    first_response: str, expected_kind: str, expected_status: int | None,
    expected_http_failures: int,
) -> None:
    """重试恢复后仍要保留失败类别，且不得保存错误响应或异常消息。"""
    from pydantic import BaseModel

    from masm.providers.llm import ModelRequest, OpenAICompatibleLLM

    class TinyResult(BaseModel):
        value: int

    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if first_response == "timeout":
                raise httpx.ReadTimeout("private-timeout-marker")
            if first_response in {"http_503", "http_302"}:
                status = 503 if first_response == "http_503" else 302
                return httpx.Response(status, json={"error": "private-error-marker"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "private-invalid-json"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"value": 7}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    llm = OpenAICompatibleLLM(
        model="test-model", base_url="https://example.invalid", api_key="private-key-marker",
    )
    text_embeddings = SimpleNamespace(_client=None, model_name="embed-model")
    runtime = SimpleNamespace(
        llm=llm, embeddings=SimpleNamespace(_text_embeddings=text_embeddings),
    )
    perception = SimpleNamespace(extract=lambda: llm.complete_json(
            ModelRequest(prompt="private-prompt-marker", model="test-model", prompt_version="v1"),
            TinyResult,
        ))
    pipeline = SimpleNamespace(
        _perception=perception, _recall=lambda: None,
        _temporal=SimpleNamespace(analyze=lambda: None),
        _curator=SimpleNamespace(propose=lambda: None),
    )
    service = SimpleNamespace(
        _pipeline=pipeline, _prepare=lambda: None,
        _repo=SimpleNamespace(finalize_request=lambda: None),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=service, search_service=SimpleNamespace(search=lambda: None),
    ))
    with metered_holdout.meter_app_stages(app):
        with metered_holdout.meter_runtime(
            runtime, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
        ) as meters:
            result = perception.extract()

    assert result.value == 7
    failures = metered_holdout.summarize_attempt_failures(meters)
    assert failures == [{
        "provider": "llm", "stage": "perception", "kind": expected_kind,
        "status_code": expected_status, "count": 1,
    }]
    assert len(meters[0].records) == 2
    assert metered_holdout.summarize_usage(meters[0].records)[
        "http_failures"
    ] == expected_http_failures
    serialized = json.dumps(failures)
    for marker in ("private-error-marker", "private-timeout-marker", "private-key-marker",
                   "private-prompt-marker", "private-invalid-json"):
        assert marker not in serialized


def test_embedding_retry_is_classified_but_preparation_stage_succeeds() -> None:
    """Embedding 重试恢复不能被阶段成功数掩盖，也不能误算为逻辑失败。"""
    from masm.providers.openai_embeddings import OpenAICompatibleEmbeddingProvider

    attempts = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "private-error-marker"})
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [1.0, 0.0]}],
            "usage": {"prompt_tokens": 2},
        })

    embedding = OpenAICompatibleEmbeddingProvider(
        model="embed-model", model_version="test", dimensions=2,
        base_url="https://example.invalid", api_key="private-key-marker",
    )
    llm = SimpleNamespace(_client=None, model="test-model")
    runtime = SimpleNamespace(
        llm=llm, embeddings=SimpleNamespace(_text_embeddings=embedding),
    )
    pipeline = SimpleNamespace(
        _perception=SimpleNamespace(extract=lambda: None), _recall=lambda: None,
        _temporal=SimpleNamespace(analyze=lambda: None),
        _curator=SimpleNamespace(propose=lambda: None),
    )
    service = SimpleNamespace(
        _pipeline=pipeline, _prepare=lambda: embedding.embed_texts(["private-text-marker"]),
        _repo=SimpleNamespace(finalize_request=lambda: None),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=service, search_service=SimpleNamespace(search=lambda: None),
    ))
    with metered_holdout.meter_app_stages(app) as stages:
        with metered_holdout.meter_runtime(
            runtime, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
        ) as meters:
            assert service._prepare() == [[1.0, 0.0]]

    assert attempts == 2
    assert embedding.records[0].attempts == 2
    assert embedding.records[0].succeeded is True
    assert metered_holdout.summarize_stage_latency(stages)["embedding_prepare"]["failures"] == 0
    assert metered_holdout.summarize_attempt_failures(meters) == [{
        "provider": "embedding", "stage": "embedding_prepare", "kind": "http_status",
        "status_code": 503, "count": 1,
    }]
    assert metered_holdout.summarize_usage(meters[1].records)["http_failures"] == 1
    serialized = json.dumps(metered_holdout.build_diagnostic_summary(stages, meters))
    for marker in ("private-error-marker", "private-key-marker", "private-text-marker"):
        assert marker not in serialized
    assert llm._client is None
    assert embedding._client is None


def test_search_rule_fallback_does_not_hide_failed_llm_attempts() -> None:
    """Search 内部降级成功时阶段不算失败，但两次模型失败仍须可见。"""
    from masm.providers.llm import OpenAICompatibleLLM
    from masm.retrieval.query_analyzer import QueryAnalyzer

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "private-error-marker"})

    llm = OpenAICompatibleLLM(
        model="test-model", base_url="https://example.invalid", api_key="private-key-marker",
    )
    runtime = SimpleNamespace(
        llm=llm, embeddings=SimpleNamespace(_text_embeddings=SimpleNamespace(
            _client=None, model_name="embed-model",
        )),
    )
    analyzer = QueryAnalyzer(llm=llm)
    search = SimpleNamespace(search=lambda query: analyzer.parse(query))
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=SimpleNamespace(_pipeline=None), search_service=search,
    ))
    with metered_holdout.meter_app_stages(app) as stages:
        with metered_holdout.meter_runtime(
            runtime, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
        ) as meters:
            parsed = search.search("Who owns private-project-marker?")

    assert parsed.text_queries == ("Who owns private-project-marker",)
    assert len(llm.records) == 1
    assert llm.records[0].attempts == 2
    assert llm.records[0].succeeded is False
    assert metered_holdout.summarize_stage_latency(stages)["search"]["failures"] == 0
    assert metered_holdout.summarize_attempt_failures(meters) == [{
        "provider": "llm", "stage": "search", "kind": "http_status",
        "status_code": 503, "count": 2,
    }]
    assert metered_holdout.summarize_usage(meters[0].records)["http_failures"] == 2
    serialized = json.dumps(metered_holdout.build_diagnostic_summary(stages, meters))
    for marker in ("private-error-marker", "private-key-marker", "private-project-marker"):
        assert marker not in serialized
    assert llm._client is None


def test_diagnostic_summary_contains_only_aggregate_stage_and_failure_fields() -> None:
    """报告必须能审阅瓶颈与失败类别，但不能输出单次请求或正文。"""
    stages = [
        metered_holdout.StageRecord("perception", 12.0, True),
        metered_holdout.StageRecord("perception", 20.0, False),
        metered_holdout.StageRecord("recall", 4.0, True),
    ]
    meter = SimpleNamespace(attempt_failures=[
        metered_holdout.AttemptFailureRecord("llm", "perception", "http_status", 503),
        metered_holdout.AttemptFailureRecord("llm", "perception", "http_status", 503),
    ])

    summary = metered_holdout.build_diagnostic_summary(stages, (meter,))

    assert summary["stage_latency_ms"]["perception"] == {
        "count": 2, "mean_ms": 16.0, "max_ms": 20.0, "failures": 1,
    }
    assert summary["stage_latency_ms"]["recall"]["count"] == 1
    assert summary["attempt_failures"] == [{
        "provider": "llm", "stage": "perception", "kind": "http_status",
        "status_code": 503, "count": 2,
    }]
    assert "private" not in json.dumps(summary)


def test_meter_runtime_restores_clients_if_provider_method_cannot_be_wrapped() -> None:
    """计量安装中途失败也不能把已注入 Client 留在 Provider 上。"""
    class LockedLLM:
        __slots__ = ("model", "_client")

        def __init__(self) -> None:
            self.model = "locked-model"
            self._client = None

        def _post(self) -> None:
            return None

    llm = LockedLLM()
    embedding = SimpleNamespace(model_name="embed-model", _client=None)
    runtime = SimpleNamespace(
        llm=llm, embeddings=SimpleNamespace(_text_embeddings=embedding),
    )
    with pytest.raises(AttributeError):
        with metered_holdout.meter_runtime(runtime):
            pass
    assert llm._client is None
    assert embedding._client is None


def test_perception_count_uses_model_output_type_not_shared_prompt_version() -> None:
    """多个 Agent 共用 v1 提示版本时仍须正确区分感知调用。"""
    calls = [
        SimpleNamespace(output_type="PerceptionResult", succeeded=True),
        SimpleNamespace(output_type="CuratorResult", succeeded=True),
        SimpleNamespace(output_type="QueryAnalysis", succeeded=False),
    ]
    assert metered_holdout.summarize_agent_calls(calls) == {
        "logical_llm_calls": 3,
        "perception_calls": 1,
        "fused_text_calls": 0,
        "failed_logical_calls": 1,
    }


def test_fused_text_stage_and_call_count_are_separate_and_content_free() -> None:
    marker = "private-source-marker"
    fused = SimpleNamespace(extract=lambda value: value)
    pipeline = SimpleNamespace(
        _perception=SimpleNamespace(extract=lambda value: value),
        _fused_text=fused,
        _recall=lambda value: value,
        _temporal=SimpleNamespace(analyze=lambda value: value),
        _curator=SimpleNamespace(propose=lambda value: value),
    )
    service = SimpleNamespace(
        _pipeline=pipeline, _prepare=lambda value: value,
        _repo=SimpleNamespace(finalize_request=lambda value: value),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        add_service=service, search_service=SimpleNamespace(search=lambda value: value),
    ))

    with metered_holdout.meter_app_stages(app) as stages:
        assert fused.extract(marker) == marker

    calls = [SimpleNamespace(output_type="FusedTextResult", succeeded=True)]
    assert [row.stage for row in stages] == ["fused_text"]
    assert marker not in repr(stages)
    assert metered_holdout.summarize_agent_calls(calls) == {
        "logical_llm_calls": 1,
        "perception_calls": 0,
        "fused_text_calls": 1,
        "failed_logical_calls": 0,
    }


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://u:private-password@db.example.org/masm_test",
    "postgresql+psycopg://u:private-password@localhost/production",
    "postgresql+psycopg://u:private-password@localhost/masm_test?host=db.example.org",
    "postgresql+psycopg://u:private-password@localhost/masm_test?hostaddr=203.0.113.10",
    "postgresql+psycopg://u:private-password@localhost/masm_test?service=production",
])
def test_test_database_guard_rejects_remote_or_production_without_leaking_url(url: str) -> None:
    """误将评估指向远程或生产数据库时必须在连接前拒绝。"""
    with pytest.raises(ValueError) as failure:
        metered_holdout.validate_test_database_url(url)
    assert "private-password" not in str(failure.value)


def test_price_card_requires_three_rates_and_source() -> None:
    """缺少任一费率或出处时不能产生看似准确的价格。"""
    with pytest.raises(ValueError):
        metered_holdout.parse_price_card("", "0.15", "0.60", "0.02")
    with pytest.raises(ValueError):
        metered_holdout.parse_price_card("official", "0.15", None, "0.02")
    prices = metered_holdout.parse_price_card(
        "official", "0.15", "0.60", "0.02",
    )
    assert prices == {
        "llm_input_usd": Decimal("0.15"),
        "llm_output_usd": Decimal("0.60"),
        "embedding_input_cny": Decimal("0.02"),
    }


def test_command_rejects_remote_database_before_starting_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """误配远程数据库时命令不能连接数据库或发送模型请求。"""
    monkeypatch.setenv(
        "MASM_TEST_DATABASE_URL",
        "postgresql+psycopg://u:private-password@db.example.org/masm_test",
    )
    with pytest.raises(ValueError, match="本机 masm_test") as failure:
        metered_holdout.main([
            "--source-root", str(tmp_path), "--version", "v1.1",
            "--asset-dir", str(tmp_path / "assets"),
            "--output", str(tmp_path / "result.json"),
            "--cleanup-output", str(tmp_path / "cleanup.tsv"),
        ])
    assert "private-password" not in str(failure.value)
    assert not (tmp_path / "result.json").exists()
