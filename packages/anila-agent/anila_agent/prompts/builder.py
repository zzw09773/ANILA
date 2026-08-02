"""組裝 agent instructions：共同前導 + system prompt + 長期記憶索引 + output style。

memdir 啟用時把 ``MEMORY.md`` 索引常駐進系統提示（讓模型知道有哪些記憶，再用
``search_memory`` 取全文）。output style 為可切換的 persona。

共同前導（NCSIST 身分／語言／國家用語／紀年／要職／資料紀律）一律排最前——
它是靜態前綴，吃 vLLM prefix cache；領域 system prompt（system.md 或 dev 自訂）
接在其後，不必重複前導已涵蓋的語言與身分規則。
"""

from __future__ import annotations

from anila_core.prompts import COMMON_PREAMBLE

from anila_agent.prompts import load_system_prompt


def build_instructions(
    *,
    system: str | None = None,
    memory_index: str | None = None,
    output_style: str | None = None,
    preamble: str | None = COMMON_PREAMBLE,
) -> str:
    """組合最終 instructions。``preamble=None`` 可關閉共同前導（測試用）。"""
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    parts.append(system if system is not None else load_system_prompt())
    if output_style:
        parts.append(f"\n# 輸出風格\n{output_style.strip()}")
    if memory_index and memory_index.strip():
        parts.append(
            "\n# 長期記憶索引（memdir）\n"
            f"{memory_index.strip()}\n\n"
            "需要某則記憶內容時，用 `search_memory` 工具以主題查詢取出全文。"
        )
    return "\n".join(parts)
