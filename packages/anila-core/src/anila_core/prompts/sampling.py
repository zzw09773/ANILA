"""任務別取樣參數表。

**已接線**：``router``（2026-09-02，`api/router_server.py` 送上游的每一通
主模型呼叫都帶這一列；呼叫端請求本身有帶 temperature／max_tokens 時以呼叫端為準）、
``chips``（``PromptSuggestion``）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SamplingDefaults:
    temperature: float
    max_tokens: int


TASK_SAMPLING: dict[str, SamplingDefaults] = {
    # Router 每一通主模型呼叫（分派判斷、直答、改寫）。使用者看不到 max_tokens；
    # 4096 會被思考變體先燒光，長 HTML 寫到一半就 length。預設對齊
    # LENGTH_RETRY_CAP，治理中心模型列仍可覆寫。
    "router": SamplingDefaults(temperature=0.3, max_tokens=32768),  # 已接線
    "chips": SamplingDefaults(temperature=0.4, max_tokens=1024),  # 已接線
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
