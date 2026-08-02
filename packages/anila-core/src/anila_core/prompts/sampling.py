"""任務別取樣參數表。

設計依據：``docs/designs/ncsist-prompt-localization-and-harness.md`` §6-5、§9b。

**目前已接線**：僅 ``chips``（``PromptSuggestion`` 經 ``get_sampling("chips")``
讀 temperature／max_tokens）。

**建議值（尚未接線）**：``rag_qa``／``chat``／``json_gen``／``title``——表內數字
供後續入口收斂時對齊；改它們**不會**改變今日執行行為。§9b 實測：gemma4
家思考變體單題 reasoning 就燒 420–1258 completion tokens，因此建議地板
偏高（chips≥1024、chat／json≥2048、rag_qa≥4096），避免
``finish_reason=length``＋空 content 的靜默失敗。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SamplingDefaults:
    temperature: float
    max_tokens: int


TASK_SAMPLING: dict[str, SamplingDefaults] = {
    "rag_qa": SamplingDefaults(temperature=0.2, max_tokens=4096),  # 建議值（尚未接線）
    "chat": SamplingDefaults(temperature=0.4, max_tokens=2048),  # 建議值（尚未接線）
    "chips": SamplingDefaults(temperature=0.4, max_tokens=1024),  # 已接線
    "json_gen": SamplingDefaults(temperature=0.15, max_tokens=2048),  # 建議值（尚未接線）
    "title": SamplingDefaults(temperature=0.3, max_tokens=1024),  # 建議值（尚未接線）
}


def get_sampling(task: str) -> SamplingDefaults:
    """Lookup ``TASK_SAMPLING[task]``; raise KeyError with known keys on miss."""
    try:
        return TASK_SAMPLING[task]
    except KeyError as exc:
        known = ", ".join(sorted(TASK_SAMPLING))
        raise KeyError(
            f"未知取樣任務 {task!r}；已知：{known}"
        ) from exc
