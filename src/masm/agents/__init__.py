"""有限职责的智能体角色（由确定性编排器调用，不互相发起无边界循环）。"""

from importlib import resources

PROMPT_PACKAGE = "masm.agents"
PROMPT_DIRNAME = "prompts"


def load_prompt(name: str) -> str:
    """读取智能体 Prompt 包资源（UTF-8），兼容源码树与安装后的 wheel。"""
    return (
        resources.files(PROMPT_PACKAGE)
        .joinpath(PROMPT_DIRNAME, name)
        .read_text(encoding="utf-8")
    )
