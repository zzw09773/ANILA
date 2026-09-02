"""兩段式產生（2026-09-02）：大綱 → 每張各自檢索 → 逐張寫內容。

單段式的問題：整份簡報只用「知識庫名稱＋preset＋補充指示」查一次、拿相似度最高的
20 段，然後一通 LLM 同時決定結構、選版型、寫內容。結果是每張都在改寫同一批段落，
版型全是條列，內容薄。

這裡分三步：
1. ``build_outline_prompt`` / ``parse_outline``：模型先只出大綱 —— 每張要證明什麼、
   證據型態（number / comparison / process / list / definition / quote / table）、
   以及一句用來檢索的問句。
2. ``gather_slide_chunks``：每張投影片拿自己的問句各查一次（top_k 小），合併去重，
   記住每張用到哪幾段（[N] 編號）。
3. ``build_content_user_prompt``：把大綱與「每張可用段落」交給第二通，照大綱寫。
   版型由證據型態決定（見 ``EVIDENCE_TO_LAYOUT``），不再靠 prompt 求模型多樣化。
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.services.llm_json import extract_json_object, loads_lenient

EVIDENCE_KINDS: tuple[str, ...] = (
    "number", "comparison", "process", "list", "definition", "quote", "table",
    "timeline", "org", "diagram",
)

# 證據型態 → 版型（給第二通的硬規則；模型仍可在同族內微調）
EVIDENCE_TO_LAYOUT: dict[str, str] = {
    "number": "stat_callout",
    "comparison": "two_column",
    "table": "table",
    "process": "process",
    "list": "icon_rows 或 standard（3-5 個並列要點用 icon_rows）",
    "definition": "standard（條文原文放 bullets，條號放 title）",
    "quote": "quote",
    "timeline": "figure（kind=timeline）",
    "org": "figure（kind=org）",
    "diagram": "figure（kind=svg，自己畫）",
}


class OutlineSlide(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    evidence: str = Field(default="list", max_length=40)
    query: str = Field(..., min_length=1, max_length=200)

    @field_validator("evidence")
    @classmethod
    def _norm_evidence(cls, v: str) -> str:
        v = (v or "").strip().lower().replace("-", "_")
        return v if v in EVIDENCE_KINDS else "list"


class OutlineSection(BaseModel):
    heading: str = Field(..., min_length=1, max_length=120)
    slides: list[OutlineSlide] = Field(..., min_length=1, max_length=12)


class Outline(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    theme: str | None = None
    sections: list[OutlineSection] = Field(..., min_length=1, max_length=8)

    def all_slides(self) -> list[OutlineSlide]:
        return [s for sec in self.sections for s in sec.slides]


def build_outline_prompt(
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    seed_chunks: list[dict[str, Any]],
    *,
    count_hint: str,
    min_slides: int,
) -> tuple[str, str]:
    system = "\n".join([
        "You are a JSON-only presentation planner. Output is parsed by a strict JSON parser.",
        "第一個字元必須是 {，最後一個字元必須是 }，不要 ``` 圍欄、不要前言。",
        "",
        "任務：為一份簡報寫**大綱**（不是內容）。輸出形狀：",
        '{"title": "...", "theme": "corporate_navy|academic_paper|warm_journal|executive_brief|startup_pitch",',
        ' "sections": [{"heading": "章節名", "slides": [',
        '     {"title": "投影片標題", "evidence": "number|comparison|process|list|definition|quote|table",',
        '      "query": "用來到文件裡找這張內容的檢索問句（6-20 字，用文件會出現的詞）"}]}]}',
        "",
        "規則：",
        f"- 內容投影片總數 **{count_hint}**（下限 {min_slides} 張；封面與章節頁另外算，不用列）。",
        "- 2-4 個章節，每章 2-5 張；章節名要像口頭報告的段落名（背景／種類／程序／救濟／注意事項）。",
        "- evidence 依「這張最有力的證據長什麼樣」挑：",
        "    number      有一個關鍵數字（期限、金額、比例）",
        "    table       兩個以上對象各有兩個以上屬性（種類×身分、方案×條件）",
        "    comparison  只有兩個對象的對照（前後、新舊、有無）",
        "    process     有先後順序的步驟、程序、流程、救濟途徑",
        "    list        3-5 個並列要點",
        "    definition  一條定義或條文原文",
        "    quote       一句值得整頁引用的話",
        "    timeline    期限、時序（送達 → 30 日 → 20 日）",
        "    org         機關層級、隸屬關係",
        "    diagram     架構、關係圖（模型自己畫 SVG）",
        "- 一份簡報至少 2 張是 timeline / org / diagram / process / table，不要全部 list。",
        "- query 要具體到能在文件裡命中：寫條文用語與名詞，不寫「介紹」「說明」這種空詞。",
        "- 不要寫封面、目錄、結語、資料來源這種投影片（系統會加）。",
        "- 使用台灣繁體中文。",
    ])
    parts = [f"知識庫名稱：{collection_name}", f"風格 preset：{preset}"]
    if seed_chunks:
        parts += ["", "以下是知識庫裡與主題最相關的段落（只用來規劃，不用逐字寫進大綱）：", ""]
        for i, c in enumerate(seed_chunks[:12], start=1):
            parts.append(f"[{i}] 來源：{c.get('filename', '?')}")
            parts.append(str(c.get("content", ""))[:600])
            parts.append("")
    if extra_instructions:
        parts += ["", f"使用者補充指示：\n{extra_instructions}"]
    return system, "\n".join(parts)


def parse_outline(raw: str) -> Outline:
    try:
        data = loads_lenient(extract_json_object(raw))
        outline = Outline.model_validate(data)
    except (ValueError, ValidationError, TypeError) as exc:
        raise ValueError(f"outline invalid: {exc}") from exc
    if not outline.all_slides():
        raise ValueError("outline has no slides")
    return outline


RetrieveFn = Callable[..., Awaitable[list[dict[str, Any]]]]


async def gather_slide_chunks(
    bearer: str,
    collection_id: int,
    outline: Outline,
    seed_chunks: list[dict[str, Any]],
    *,
    retrieve: RetrieveFn,
    per_slide_k: int = 4,
    cap: int = 36,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """每張投影片用自己的 query 各查一次；回 (合併去重後的 chunks, 每張的 [N] 索引)。

    seed chunks 排最前面（它們是主題整體最相關的），之後照投影片順序追加新命中；
    同一段（chunk_key）只出現一次；總數超過 ``cap`` 後新段落不再加入，但既有段落
    仍可被後面的投影片引用。
    """
    chunks: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}

    def _add(c: dict[str, Any]) -> int | None:
        key = str(c.get("chunk_key") or f"{c.get('filename')}#{len(chunks)}")
        if key in index_by_key:
            return index_by_key[key]
        if len(chunks) >= cap:
            return None
        chunks.append(c)
        index_by_key[key] = len(chunks)  # 1-based
        return index_by_key[key]

    for c in seed_chunks:
        _add(c)

    per_slide: list[list[int]] = []
    for slide in outline.all_slides():
        refs: list[int] = []
        try:
            hits = await retrieve(bearer, collection_id, slide.query, top_k=per_slide_k)
        except Exception:  # noqa: BLE001 — 一張查不到就用種子段落，不炸整份
            hits = []
        for h in hits:
            n = _add(h)
            if n is not None and n not in refs:
                refs.append(n)
        per_slide.append(refs)
    return chunks, per_slide


def build_content_user_prompt(
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    outline: Outline,
    chunks: list[dict[str, Any]],
    per_slide: list[list[int]],
) -> str:
    parts = [
        f"知識庫名稱：{collection_name}",
        f"風格 preset：{preset}",
        "",
        "── 大綱（照這個順序與標題寫；每章開頭加一張 section_break，title 用章節名）──",
    ]
    i = 0
    for sec in outline.sections:
        parts.append(f"章節：{sec.heading}")
        for s in sec.slides:
            refs = per_slide[i] if i < len(per_slide) else []
            ref_str = " ".join(f"[{n}]" for n in refs) if refs else "（沒有專屬段落，用整體段落）"
            parts.append(
                f"  投影片 {i + 1}：{s.title}（證據：{s.evidence} → 版型 {EVIDENCE_TO_LAYOUT.get(s.evidence, 'standard')}）"
                f"  可用段落：{ref_str}"
            )
            i += 1
    parts += ["", "── 檢索到的段落（每張投影片優先用自己的「可用段落」，引用寫 (參 [N]) 放在 speaker_notes）──", ""]
    for n, c in enumerate(chunks, start=1):
        parts.append(f"[{n}] 來源：{c.get('filename', '?')}（chunk {c.get('chunk_key', '?')}，相似度 {float(c.get('score', 0) or 0):.3f}）")
        parts.append(str(c.get("content", "")))
        parts.append("")
    if extra_instructions:
        parts += ["", f"使用者補充指示：\n{extra_instructions}"]
    return "\n".join(parts)


TWO_PASS_SYSTEM_ADDENDUM = "\n".join([
    "",
    "── 兩段式規則（本次已有大綱，以下優先於上面的通則）──",
    "- 投影片順序、標題、數量照大綱；每個章節開頭加一張 section_break（title=章節名）；",
    "  第一張仍是整份簡報的封面 section_break（規則 1）。",
    "- 每張的版型由大綱的「證據」決定：number→stat_callout、table→table、comparison→two_column、",
    "  process→process、quote→quote、list→icon_rows（3-5 點）或 standard、definition→standard、",
    "  timeline→figure(timeline)、org→figure(org)、diagram→figure(svg)。",
    "- 每張都要有 key_message（≤ 40 字的結論）；3-4 條 bullet、每條 ≤ 35 字。",
    "- 每張只用自己的「可用段落」寫；沒有專屬段落才用整體段落。內容要具體到條號、期限、",
    "  對象；不要把同一段話換句話說塞到好幾張。",
    "- 不要自己寫「資料來源」頁，系統會加。",
])
