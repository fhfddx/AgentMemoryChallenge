"""有限职责的智能体角色（由确定性编排器调用，不互相发起无边界循环）。"""

from importlib import resources

PROMPT_PACKAGE = "masm.agents"
PROMPT_DIRNAME = "prompts"

# 历史候选硬上限：智能体上下文预算的集中定义，任何组件级配置都不能突破。
MAX_HISTORY = 8


def resolve_max_history(value: int | None = None) -> int:
    """把历史候选上限约束到 ``0..MAX_HISTORY``。

    负数属于无效配置，明确拒绝；超过硬上限属于越界配置，安全截断。
    ``0`` 是合法值，表示不召回也不传入任何历史。
    """
    if value is None:
        return MAX_HISTORY
    if value < 0:
        raise ValueError("max_history 必须为非负整数")
    return min(value, MAX_HISTORY)


def load_prompt(name: str) -> str:
    """读取智能体 Prompt 包资源（UTF-8），兼容源码树与安装后的 wheel。"""
    return (
        resources.files(PROMPT_PACKAGE)
        .joinpath(PROMPT_DIRNAME, name)
        .read_text(encoding="utf-8")
    )
