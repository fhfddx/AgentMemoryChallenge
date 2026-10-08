"""比赛运行档位与模型配置测试。"""

from dataclasses import replace

import pytest

from masm.config import RuntimeProfile, Settings


def _official(profile: RuntimeProfile = RuntimeProfile.OFFICIAL_BASELINE) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://postgres@localhost/masm",
        runtime_profile=profile,
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-secret",
        llm_model="gpt-4o-mini",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="embedding-secret",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
        model_timeout_seconds=30.0,
        model_max_attempts=2,
    )


def test_local_fake_never_requires_model_credentials() -> None:
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")

    settings.validate_runtime()

    assert settings.runtime_profile is RuntimeProfile.LOCAL_FAKE
    assert settings.is_official is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("llm_api_key", ""), ("embedding_api_key", "")],
)
def test_official_profiles_require_both_provider_credentials(
    field: str, value: str
) -> None:
    settings = replace(_official(), **{field: value})

    with pytest.raises(ValueError, match="模型配置不完整"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_model", "other-llm"),
        ("embedding_model", "other-embedding"),
        ("llm_base_url", "http://llm.example/v1"),
        ("embedding_base_url", "http://embedding.example/v1"),
    ],
)
def test_official_profiles_reject_wrong_models_and_non_https_urls(
    field: str, value: str
) -> None:
    settings = replace(_official(), **{field: value})

    with pytest.raises(ValueError):
        settings.validate_runtime()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embedding_dimensions", 0),
        ("model_timeout_seconds", 0.0),
        ("model_max_attempts", 0),
        ("model_max_attempts", 3),
        ("max_concurrent_requests", 0),
        ("max_requests_per_minute", 0),
        ("min_text_similarity", float("nan")),
        ("min_text_similarity", 1.01),
        ("min_image_similarity", -1.01),
        ("min_lexical_rank", 0.0),
        ("min_lexical_rank", float("inf")),
        ("min_lexical_rank", 1.01),
        ("selector_max_candidates", 0),
        ("selector_max_candidates", 33),
        ("selector_max_selected", 0),
        ("selector_max_selected", 13),
        ("selector_max_chars_per_candidate", 0),
        ("selector_max_chars_per_candidate", 4097),
        ("selector_timeout_seconds", 0.0),
        ("selector_timeout_seconds", float("inf")),
        ("selector_timeout_seconds", 31.0),
    ],
)
def test_model_limits_require_positive_dimensions_timeout_and_one_or_two_attempts(
    field: str, value: int | float
) -> None:
    settings = replace(_official(), **{field: value})

    with pytest.raises(ValueError):
        settings.validate_runtime()


def test_settings_repr_redacts_provider_credentials() -> None:
    rendered = repr(_official())

    assert "llm-secret" not in rendered
    assert "embedding-secret" not in rendered


def test_selected_limit_cannot_exceed_candidate_limit() -> None:
    with pytest.raises(ValueError):
        replace(_official(), selector_max_candidates=5, selector_max_selected=6).validate_runtime()


def test_from_env_reads_runtime_provider_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DATABASE_URL": "postgresql+psycopg://postgres@localhost/masm",
        "MASM_RUNTIME_PROFILE": "official-masm",
        "MASM_LLM_BASE_URL": "https://llm.example/v1",
        "MASM_LLM_API_KEY": "llm-secret",
        "MASM_LLM_MODEL": "gpt-4o-mini",
        "MASM_EMBEDDING_BASE_URL": "https://embedding.example/v1",
        "MASM_EMBEDDING_API_KEY": "embedding-secret",
        "MASM_EMBEDDING_MODEL": "text-embedding-v4",
        "MASM_EMBEDDING_DIMENSIONS": "1024",
        "MASM_MODEL_TIMEOUT_SECONDS": "45.5",
        "MASM_MODEL_MAX_ATTEMPTS": "1",
        "MASM_MAX_CONCURRENT_REQUESTS": "16",
        "MASM_MAX_REQUESTS_PER_MINUTE": "1000",
        "MASM_MIN_TEXT_SIMILARITY": "0.51",
        "MASM_MIN_IMAGE_SIMILARITY": "0.43",
        "MASM_MIN_LEXICAL_RANK": "0.002",
        "MASM_EVIDENCE_SELECTOR_ENABLED": "0",
        "MASM_SELECTOR_MAX_CANDIDATES": "24",
        "MASM_SELECTOR_MAX_SELECTED": "8",
        "MASM_SELECTOR_MAX_CHARS_PER_CANDIDATE": "900",
        "MASM_SELECTOR_TIMEOUT_SECONDS": "20",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()
    settings.validate_runtime()

    assert settings.runtime_profile is RuntimeProfile.OFFICIAL_MASM
    assert settings.is_official is True
    assert settings.embedding_dimensions == 1024
    assert settings.model_timeout_seconds == 45.5
    assert settings.model_max_attempts == 1
    assert settings.max_concurrent_requests == 16
    assert settings.max_requests_per_minute == 1000
    assert settings.min_text_similarity == 0.51
    assert settings.min_image_similarity == 0.43
    assert settings.min_lexical_rank == 0.002
    assert settings.selector_enabled is False
    assert settings.selector_max_candidates == 24
    assert settings.selector_max_selected == 8
    assert settings.selector_max_chars_per_candidate == 900
    assert settings.selector_timeout_seconds == 20


def test_fused_text_flag_is_off_unless_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MASM_FUSED_EMPTY_HISTORY_TEXT", raising=False)
    assert Settings.from_env().fused_empty_history_text is False
    monkeypatch.setenv("MASM_FUSED_EMPTY_HISTORY_TEXT", "1")
    assert Settings.from_env().fused_empty_history_text is True
    monkeypatch.setenv("MASM_FUSED_EMPTY_HISTORY_TEXT", "true")
    assert Settings.from_env().fused_empty_history_text is False
