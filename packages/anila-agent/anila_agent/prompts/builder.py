"""組裝 agent instructions：system prompt + 長期記憶索引 + output style。

memdir 啟用時把 ``MEMORY.md`` 索引常駐進系統提示（讓模型知道有哪些記憶，再用
``search_memory`` 取全文）。output style 為可切換的 persona。
"""

from __future__ import annotations

from anila_agent.prompts import load_system_prompt


def build_instructions(
    *,
    system: str | None = None,
    memory_index: str | None = None,
    output_style: str | None = None,
) -> str:
    """組合最終 instructions。"""
    parts: list[str] = [system if system is not None else load_system_prompt()]
    if output_style:
        parts.append(f"\n# 輸出風格\n{output_style.strip()}")
    if memory_index and memory_index.strip():
        parts.append(
            "\n# 長期記憶索引（memdir）\n"
            f"{memory_index.strip()}\n\n"
            "需要某則記憶內容時，用 `search_memory` 工具以主題查詢取出全文。"
        )
    return "\n".join(parts)
