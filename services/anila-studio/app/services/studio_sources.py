"""結尾「資料來源」頁 — 由管線從檢索段落產生，不是模型寫的。

法規簡報的可信度來自「哪一份文件、哪幾段」；模型寫的引用會漂，管線寫的不會。
每份文件一行：檔名 + 用到的 chunk 標記；同一份文件只列一次，順序照第一次出現。
"""
from __future__ import annotations

import re
from typing import Any

from app.schemas.studio import Slide, SlidesSpec, SourceItem

SOURCES_TITLE = "資料來源"

_REF_GROUP_RE = re.compile(r"[\[【]\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[\]】]")
_MAX_NAMED = 3
_ARTICLE_RE = re.compile(r"第[-\s]*(\d+)[-\s]*條")


def _describe_chunks(keys: list[str]) -> str | None:
    """Chunk keys like ``leaf-00002-第-3-條`` become 「第 3 條」; anything else
    stays a short 「段落 …」 list. Readers care about articles, not keys."""
    articles: list[str] = []
    for k in keys:
        m = _ARTICLE_RE.search(k)
        if m:
            label = f"第 {m.group(1)} 條"
            if label not in articles:
                articles.append(label)
    if articles:
        return "、".join(articles[:10])
    if keys:
        return ("段落 " + "、".join(keys[:8]))[:200]
    return None


def _cited_numbers(texts: list[str]) -> list[int]:
    seen: list[int] = []
    for t in texts:
        for group in _REF_GROUP_RE.findall(t or ""):
            for n in re.split(r"[,，、]", group):
                try:
                    v = int(n.strip())
                except ValueError:
                    continue
                if v not in seen:
                    seen.append(v)
    return seen


def _slide_texts(slide: Slide) -> list[str]:
    out: list[str] = [slide.title, *slide.bullets]
    if slide.stat:
        out += [slide.stat.label, slide.stat.supporting]
    if slide.quote:
        out += [slide.quote.text, slide.quote.attribution or ""]
    if slide.columns:
        for c in slide.columns:
            out += [c.heading, *c.bullets]
    if slide.icon_rows:
        for r in slide.icon_rows:
            out += [r.heading, r.description]
    if slide.steps:
        for st in slide.steps:
            out += [st.heading, st.description]
    if slide.table:
        out += [*slide.table.columns, *[cell for row in slide.table.rows for cell in row]]
    return [t for t in out if t]


def attach_source_lines(spec: SlidesSpec, chunks: list[dict[str, Any]]) -> SlidesSpec:
    """Fill ``Slide.source_line`` from the ``[N]`` references on each content
    slide. Must run BEFORE ``normalize_spec`` (which strips the references).
    Out-of-range numbers are ignored; more than three files collapse to
    「等 N 份文件」so the footer stays one line."""
    if not chunks:
        return spec
    slides: list[Slide] = []
    for slide in spec.slides:
        if slide.layout_kind in ("section_break", "sources"):
            slides.append(slide)
            continue
        names: list[str] = []
        for n in _cited_numbers(_slide_texts(slide)):
            if 1 <= n <= len(chunks):
                name = str(chunks[n - 1].get("filename") or "").strip()
                if name and name not in names:
                    names.append(name)
        if not names:
            slides.append(slide)
            continue
        if len(names) > _MAX_NAMED:
            line = "資料來源：" + "、".join(names[:_MAX_NAMED - 1]) + f" 等 {len(names)} 份文件"
        else:
            line = "資料來源：" + "、".join(names)
        slides.append(slide.model_copy(update={"source_line": line[:160]}))
    return spec.model_copy(update={"slides": slides})


def append_sources_slide(spec: SlidesSpec, chunks: list[dict[str, Any]]) -> SlidesSpec:
    """Return ``spec`` with one closing ``sources`` slide; unchanged when there is
    nothing to cite or the deck already ends with one."""
    if not chunks:
        return spec
    if spec.slides and spec.slides[-1].layout_kind == "sources":
        return spec
    by_file: dict[str, list[str]] = {}
    for c in chunks:
        name = str(c.get("filename") or "").strip()
        if not name:
            continue
        key = str(c.get("chunk_key") or "").strip()
        by_file.setdefault(name, [])
        if key and key not in by_file[name]:
            by_file[name].append(key)
    if not by_file:
        return spec
    items = [
        SourceItem(label=name[:160], note=_describe_chunks(keys))
        for name, keys in list(by_file.items())[:14]
    ]
    closing = Slide(
        title=SOURCES_TITLE,
        layout_kind="sources",
        bullets=["本簡報內容依下列文件整理"],
        sources=items,
        speaker_notes="本頁由系統依檢索到的文件自動列出；條號與數字請以原文為準。",
    )
    return spec.model_copy(update={"slides": [*spec.slides, closing]})
