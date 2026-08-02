"""任務別模型路由（thinking／fast 變體）。

設計依據：``docs/designs/ncsist-prompt-localization-and-harness.md`` §9b-3。

部署上常有成對變體（例：``gemma26``＝思考版、``gemma26-nothink``＝fast）。
輔助任務（chips／標題／JSON 生成）用 fast 變體品質不輸、熱機約 0.8s、
零 reasoning 燒耗；分析型 QA 才值得留思考版。**變體名稱因部署而異，
一律放環境變數**（``ANILA_MODEL_ANALYSIS``／``ANILA_MODEL_FAST``），
程式碼只定 task→class 契約，不寫死模型名。

⚠ **親和性**：同一請求鏈內交替呼叫兩變體會付出 10–16s 換模冷啟成本
（§9b-4 實測）。輔助任務應整段黏在 fast；分析任務黏在 thinking——
不要為了單一 chips 呼叫在主對話途中切過去再切回來。

**接線狀態（合併時接完）**：
- 待接：chips hook——接線位置待定——建立 QueryEngine 並呼叫
  ``add_post_turn_hook(...)`` 的 composition root；hook 本體在
  ``post_turn/prompt_suggestion.py``。接線時把 ``model=`` 改走
  ``resolve_model("chips", default=...)``。
- 待接：``apps/anila-shell/src/app.jsx`` ``generateConversationTitle``
  （目前 ``model: effectiveTarget`` 跟主對話同一顆）。
- Studio 投影片／VLM 走獨立 env（``ANILA_STUDIO_*_MODEL``），見
  ``services/anila-studio/app/services/studio_config.py`` 與
  ``docs/runbooks/model-variants.md``。

未設 env 時 ``resolve_model`` 回傳呼叫端的 ``default``——**零行為變化**。
"""

from __future__ import annotations

import os
from typing import Literal

TaskClass = Literal["analysis", "fast"]

# 鍵集合對齊 wt/hx-guards 即將引入的 ``prompts/sampling.py`` 取樣表
# （``TASK_SAMPLING``）；合併後由條件測試強制兩邊一致。
TASK_CLASS: dict[str, TaskClass] = {
    "rag_qa": "analysis",
    "chat": "analysis",
    "chips": "fast",
    "json_gen": "fast",
    "title": "fast",
}

_ENV_FOR_CLASS: dict[TaskClass, str] = {
    "analysis": "ANILA_MODEL_ANALYSIS",
    "fast": "ANILA_MODEL_FAST",
}


def resolve_model(task: str, *, default: str) -> str:
    """依任務 class 讀對應 env；未設或空字串則回傳 ``default``。

    Raises:
        KeyError: ``task`` 不在 ``TASK_CLASS``（訊息列出已知鍵）。
    """
    try:
        task_class = TASK_CLASS[task]
    except KeyError as exc:
        known = ", ".join(sorted(TASK_CLASS))
        raise KeyError(
            f"未知模型路由任務 {task!r}；已知：{known}"
        ) from exc

    env_name = _ENV_FOR_CLASS[task_class]
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    return default
