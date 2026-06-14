"""Prompt 載入。"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent

_FALLBACK_SYSTEM = (
    "你是 ANILA 平台的檢索問答助理。回答前先用 search_documents 查知識庫，"
    "以檢索到的片段接地作答，不臆造，並以繁體中文回應。"
)


def load_system_prompt() -> str:
    """讀取 system.md；缺檔則回退內建預設。"""
    path = _PROMPTS_DIR / "system.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or _FALLBACK_SYSTEM
    except OSError:
        return _FALLBACK_SYSTEM
