"""任務別取樣參數表。

設計依據：``docs/designs/ncsist-prompt-localization-and-harness.md`` §6-5、§9b。

**已接線**：``router``（2026-09-02，`api/router_server.py` 送上游的每一通
主模型呼叫都帶這一列；呼叫端請求本身有帶 temperature／max_tokens 時以呼叫端為準）、
``chips``（``PromptSuggestion``）。

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
    # Router 每一通主模型呼叫（分派判斷、直答、改寫）。Router 分不出這一回合是
    # 規章問答還是閒聊，取兩者之間；max_tokens 取 rag_qa 的地板——思考變體單題
    # reasoning 就會燒掉 1,000+ tokens（§9b-2 實測），給少了正文會是空的。
    "router": SamplingDefaults(temperature=0.3, max_tokens=4096),  # 已接線
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
