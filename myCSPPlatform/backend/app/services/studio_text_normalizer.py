"""繁體中文 + 台灣用語後處理 — 跑在 SlidesSpec validate 之後、render 之前。

## 為什麼要做

研究 (`compass_artifact_*.md`) 第 33-39 行明確指出：Gemma 4 訓練資料以
簡中為主，繁中輸出時頻繁混入「視頻、軟件、網絡、激光、信息、鼠標、
分辨率、打印、登錄、文件、程序、內存」等大陸用詞的繁體寫法。Twinkle AI
台灣社群也報告過同類現象。LLM 端 prompt 雖會列對映表強制台灣用語，但
單靠 prompt 命中率不會 100%，必須加上確定性的後處理層做兜底。

## 為什麼選 s2twp.json

OpenCC 提供多個 config，常用的有：
- `s2t.json`     簡 → 繁（純字符轉換、不替換用詞）
- `s2tw.json`    簡 → 繁（含台灣字形差異，如 為 → 爲，但**不**替換用詞）
- `s2twp.json`   簡 → 繁（含台灣字形 + 台灣**用詞**轉換）← 我們用這個

s2twp 是最積極的版本，不只把「视频」轉成「視頻」，還會進一步把「視頻」
轉成「影片」。這正是我們要的：把潛在的簡中污染同時在「字符」與「用詞」
兩個層面修掉。

## 為什麼純 Python 套件

opencc-python-reimplemented 是純 Python 實作，不依賴系統 libopencc。
對 air-gapped 容器這代表少一層 native dependency；速度比 C++ 慢但
用在 Studio 一次處理 ≤30 張 slide × 每張 ≤3 KB 文字總共 ~50ms 內，
比 LLM call 快好幾個量級，幾乎無感。

## 為什麼跑在 spec validate 之後而非 render 端

兩個理由：
1. SlidesSpec 是後續所有處理的單一真相來源 — vision QA 也吃 spec、
   重渲也吃 spec。在這裡正規化一次，下游所有環節都拿到乾淨的繁中。
2. Renderer 是 Node 服務，引一層 OpenCC JS 等於多一個 vendor。Python
   端做簡化整體棧。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from opencc import OpenCC

from app.schemas.studio import (
    Column,
    IconRow,
    Quote,
    Slide,
    SlidesSpec,
    Stat,
)

logger = logging.getLogger(__name__)


# OpenCC instances are cheap to create but the dictionary load is ~30-50 ms.
# Cache so repeated normalize() calls share one warm instance per process.
@lru_cache(maxsize=1)
def _get_converter() -> OpenCC:
    return OpenCC("s2twp")


def _convert(text: str | None) -> str | None:
    """Run a single string through OpenCC s2twp; pass through None unchanged.

    Empty strings stay empty (the converter would return "" too, but we
    short-circuit to skip the dict lookup).
    """
    if text is None or text == "":
        return text
    converter = _get_converter()
    converted = converter.convert(text)
    return converted


def _normalize_slide(slide: Slide) -> Slide:
    """Return a NEW Slide with every string field s2twp-normalised.

    Pydantic models are immutable in spirit — we use `model_copy(update=...)`
    so callers can't accidentally observe a half-mutated state. Layout-
    specific payloads (stat / quote / columns / icon_rows) are recursed
    through; their own pydantic models get the same treatment.
    """
    patch: dict[str, Any] = {
        "title": _convert(slide.title),
        "bullets": [_convert(b) or "" for b in slide.bullets],
        "speaker_notes": _convert(slide.speaker_notes),
    }

    if slide.stat is not None:
        patch["stat"] = Stat(
            value=slide.stat.value,  # value is usually "47%" / "3.5×" — leave digits / units alone
            label=_convert(slide.stat.label) or "",
            supporting=_convert(slide.stat.supporting),
        )

    if slide.quote is not None:
        patch["quote"] = Quote(
            text=_convert(slide.quote.text) or "",
            attribution=_convert(slide.quote.attribution),
        )

    if slide.columns is not None:
        patch["columns"] = [
            Column(
                heading=_convert(c.heading) or "",
                bullets=[_convert(b) or "" for b in c.bullets],
            )
            for c in slide.columns
        ]

    if slide.icon_rows is not None:
        patch["icon_rows"] = [
            IconRow(
                # `concept` is a fixed-set semantic keyword (data_pipeline,
                # security, ...) — never localise it; that would break the
                # renderer's CONCEPT_MAP lookup.
                concept=ir.concept,
                heading=_convert(ir.heading) or "",
                description=_convert(ir.description) or "",
            )
            for ir in slide.icon_rows
        ]

    return slide.model_copy(update=patch)


def normalize_spec(spec: SlidesSpec) -> SlidesSpec:
    """Normalise every user-visible string in a SlidesSpec.

    Returns a new SlidesSpec; the input is not mutated. Field-level
    decisions:
      - title (top + per-slide)         → convert
      - bullets                          → convert each
      - speaker_notes                    → convert
      - stat.value                       → DO NOT convert (numeric, units)
      - stat.label / supporting          → convert
      - quote.text / attribution         → convert
      - columns[].heading / bullets      → convert
      - icon_rows[].concept              → DO NOT convert (semantic keyword;
                                          must match renderer CONCEPT_MAP)
      - icon_rows[].heading / description→ convert
      - palette / layout_kind            → DO NOT convert (enum-like)

    Logged at info level so we can diff before/after for any title that
    actually changed; useful for telemetry on Gemma 4's CJK regressions
    after deploys.
    """
    converted_title = _convert(spec.title) or spec.title
    if converted_title != spec.title:
        logger.info(
            "Studio s2twp normalised top-level title: %r → %r",
            spec.title, converted_title,
        )

    return spec.model_copy(
        update={
            "title": converted_title,
            "slides": [_normalize_slide(s) for s in spec.slides],
        }
    )
