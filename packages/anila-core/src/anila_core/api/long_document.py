"""超長回答改存成對話附件時，用來判斷與組出聊天裡留下的摘要。"""
from __future__ import annotations

import re

# 固定在程式裡。不讀環境變數，也不做 Console 設定。
LONG_ANSWER_CHAR_LIMIT = 7000
LONG_ANSWER_SUMMARY_CHARS = 600

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def should_store_as_document(text: str, *, continuation_exhausted: bool) -> bool:
    """超過 7000 字，或自動續寫用完仍沒寫完，才存成文件。"""
    body = text or ""
    if len(body) > LONG_ANSWER_CHAR_LIMIT:
        return True
    return bool(continuation_exhausted and body.strip())


def _heading_lines(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    in_fence = False
    for raw in (text or "").splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = _HEADING.match(raw.strip())
        if matched:
            title = matched.group(2).strip()
            if title:
                found.append((len(matched.group(1)), title))
    return found


def document_title(text: str) -> str:
    headings = _heading_lines(text)
    if headings:
        return headings[0][1]
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:40]
    return "回答"


def document_filename(text: str) -> str:
    title = document_title(text)
    cleaned = _BAD_FILENAME.sub("", title).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3].rstrip(".")
    if not cleaned:
        cleaned = "回答"
    return f"{cleaned[:40]}.md"


def opening_summary(text: str, limit: int = LONG_ANSWER_SUMMARY_CHARS) -> str:
    chunks = [
        part.strip()
        for part in re.split(r"\n\s*\n", (text or "").strip())
        if part.strip()
    ]
    if not chunks:
        return ""
    chosen = chunks[0]
    if _HEADING.match(chosen) and len(chunks) > 1 and "\n" not in chosen:
        chosen = f"{chosen}\n\n{chunks[1]}"
    if len(chosen) <= limit:
        return chosen
    cut = max(limit - 1, 1)
    return chosen[:cut].rstrip() + "…"


def table_of_contents(text: str) -> str:
    headings = _heading_lines(text)
    if not headings:
        return ""
    lines = ["目錄"]
    for level, title in headings:
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {title}")
    return "\n".join(lines)


def chat_preview(text: str) -> str:
    summary = opening_summary(text)
    toc = table_of_contents(text)
    if summary and toc:
        return f"{summary}\n\n{toc}"
    return summary or toc or "完整內容在文件裡。"
