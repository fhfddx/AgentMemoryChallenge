"""有限职责的智能体角色（由确定性编排器调用，不互相发起无边界循环）。"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """读取智能体 Prompt 文件（UTF-8）。"""
    return (PROMPT_DIR / name).read_text(encoding="utf-8")
