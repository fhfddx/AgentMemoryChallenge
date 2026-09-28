"""感知智能体单元测试（只使用 Fake Provider，不访问付费 API）。"""

import pytest

from masm.agents.perception import PerceptionAgent
from masm.providers.fakes import FakeStructuredLLM
from masm.schemas.agents import PerceptionResult
from masm.schemas.content import ImageURLPart, TextPart

_DATA_URL = "data:image/png;base64,iVBORw0KGgo="

# Prompt 中不得出现基准题目或答案示例。
_FORBIDDEN_PROMPT_MARKERS = (
    "answer:",
    "correct answer",
    "example question",
    "expected output",
    "benchmark",
    "q:",
)


def _text(content: str) -> TextPart:
    return TextPart(text=content)


def _image(url: str = _DATA_URL) -> ImageURLPart:
    return ImageURLPart(image_url={"url": url})


def _perception(**overrides: object) -> PerceptionResult:
    payload: dict = {"language": "en", "modality": "text", "keywords": ("bicycle",)}
    payload.update(overrides)
    return PerceptionResult(**payload)


def test_extract_returns_schema_object() -> None:
    agent = PerceptionAgent(FakeStructuredLLM([_perception()]))

    result = agent.extract([_text("a red bicycle")])

    assert isinstance(result, PerceptionResult)
    assert result.keywords == ("bicycle",)


def test_extract_preserves_content_order() -> None:
    """图片与文本分片必须保持原始顺序传给模型。"""
    llm = FakeStructuredLLM([_perception(modality="mixed")])
    agent = PerceptionAgent(llm)

    agent.extract([_text("first"), _image(), _text("second")])

    content = llm.requests[0].payload["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "text"]
    assert content[0]["text"] == "first"
    assert content[2]["text"] == "second"
    assert content[1]["image_url"]["url"] == _DATA_URL


def test_extract_records_model_and_prompt_version() -> None:
    llm = FakeStructuredLLM([_perception()], model="gpt-4o-mini")
    agent = PerceptionAgent(llm)

    agent.extract([_text("hello")])

    request = llm.requests[0]
    assert request.model == "gpt-4o-mini"
    assert request.prompt_version == "v1"
    assert request.prompt == agent.prompt


def test_provider_failure_propagates() -> None:
    agent = PerceptionAgent(FakeStructuredLLM([RuntimeError("provider down")]))

    with pytest.raises(RuntimeError):
        agent.extract([_text("hello")])


def test_agent_rejects_repository_injection() -> None:
    """智能体只接受 Provider 与配置，不接收 Repository 或数据库会话。"""
    llm = FakeStructuredLLM()

    with pytest.raises(TypeError):
        PerceptionAgent(llm, repository=object())
    with pytest.raises(TypeError):
        PerceptionAgent(llm, session=object())


def test_prompt_forbids_unobservable_inference() -> None:
    """Prompt 必须明确禁止推断不可观察事实。"""
    prompt = PerceptionAgent(FakeStructuredLLM()).prompt.lower()

    assert "do not infer" in prompt
    assert "not directly observable" in prompt
    assert "outside knowledge" in prompt


def test_prompt_contains_no_benchmark_examples() -> None:
    prompt = PerceptionAgent(FakeStructuredLLM()).prompt.lower()

    for marker in _FORBIDDEN_PROMPT_MARKERS:
        assert marker not in prompt
