"""Coverage expand: leftover sources grow a long-form deck; 口講 stays 5."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.studio import Slide, SlidesSpec
from app.services.studio_expand import (
    cited_chunk_indexes,
    expand_add_count,
    expand_undercovered_deck,
    merge_expanded_slides,
    should_expand_deck,
    uncovered_chunks,
)
from app.services.studio_llm import build_expand_slides_prompt, build_generation_prompt


def _slide(title: str, bullets: list[str] | None = None, **kw) -> Slide:
    return Slide(
        title=title,
        bullets=bullets or ["一個主張"],
        speaker_notes=kw.pop("speaker_notes", "上台會講的一句"),
        **kw,
    )


def _chunk(i: int, extra: str = "") -> dict:
    return {
        "filename": f"src-{i}.pdf",
        "chunk_key": f"c-{i:04d}",
        "content": (
            f"第 {i} 段還有沒被簡報講到的主張與數字 12.{i}%，"
            f"以及一節獨立的方法說明，必須另開一頁才講得完。{extra}"
        ),
        "score": 0.8,
    }


def _deck(n: int, cite: str = " (參 [1])") -> SlidesSpec:
    return SlidesSpec.model_validate({
        "title": "院內簡報",
        "theme": "corporate_navy",
        "slides": [
            _slide(f"主張{i}", [f"內容{cite}" if i == 0 else "內容"]).model_dump()
            for i in range(n)
        ],
    })


def test_undercovered_sources_should_expand_detailed_deck() -> None:
    spec = _deck(12)
    chunks = [_chunk(i) for i in range(1, 9)]
    leftover = uncovered_chunks(spec, chunks)
    assert 1 in cited_chunk_indexes(spec)
    assert len(leftover) >= 2
    assert should_expand_deck("詳細簡報", spec, chunks) is True
    assert should_expand_deck("經典報告結構", spec, chunks) is True


def test_do_not_pad_spoken_deck() -> None:
    spec = _deck(5)
    chunks = [_chunk(i) for i in range(1, 9)]
    assert should_expand_deck("口講用短頁", spec, chunks) is False
    assert should_expand_deck("Lightning Talk", spec, chunks) is False
    assert should_expand_deck("閃電簡報", spec, chunks) is False


def test_fully_cited_detailed_deck_does_not_expand() -> None:
    slides = [
        _slide(f"主張{i}", [f"內容 (參 [{i + 1}])"]).model_dump()
        for i in range(12)
    ]
    spec = SlidesSpec.model_validate({
        "title": "院內簡報",
        "theme": "corporate_navy",
        "slides": slides,
    })
    chunks = [_chunk(i) for i in range(1, 13)]
    assert uncovered_chunks(spec, chunks) == []
    assert should_expand_deck("詳細簡報", spec, chunks) is False


def test_thin_eleven_page_deck_with_leftover_grows() -> None:
    spec = _deck(11)
    chunks = [_chunk(i) for i in range(1, 7)]
    assert should_expand_deck("詳細簡報", spec, chunks) is True
    assert expand_add_count(spec, uncovered_chunks(spec, chunks)) >= 4


def test_merge_inserts_before_takeaway_and_skips_filler() -> None:
    spec = SlidesSpec.model_validate({
        "title": "T",
        "theme": "corporate_navy",
        "slides": [
            _slide("封面主張", ["副標"]).model_dump(),
            _slide("已有論點", ["舊 (參 [1])"]).model_dump(),
            _slide("收束", ["帶走"]).model_dump(),
        ],
    })
    incoming = [
        _slide("已有論點", ["重複"], citation_refs=[2]),
        _slide("沒有來源的空話", ["填料"]),
        _slide("新的方法主張", ["步驟 (參 [2])"], citation_refs=[2]),
        _slide("另一個數字", ["降了 (參 [3])"], citation_refs=[3]),
    ]
    merged = merge_expanded_slides(
        spec, incoming, valid_chunk_indexes={1, 2, 3},
    )
    titles = [s.title for s in merged.slides]
    assert titles[0] == "封面主張"
    assert titles[-1] == "收束"
    assert "新的方法主張" in titles
    assert "另一個數字" in titles
    assert titles.count("已有論點") == 1
    assert "沒有來源的空話" not in titles
    assert len(merged.slides) == 5


def test_merge_respects_thirty_cap() -> None:
    spec = _deck(29)
    incoming = [
        _slide("還能加一頁", ["主張 (參 [2])"], citation_refs=[2]),
        _slide("不該再加", ["主張 (參 [3])"], citation_refs=[3]),
    ]
    merged = merge_expanded_slides(
        spec, incoming, valid_chunk_indexes={1, 2, 3},
    )
    assert len(merged.slides) == 30
    assert merged.slides[-2].title == "還能加一頁"


def test_expand_prompt_asks_for_new_pages_only() -> None:
    system, user = build_expand_slides_prompt(
        "院內簡報",
        ["封面", "收束"],
        [{"index": 2, "filename": "a.pdf", "content": "沒講到的方法段" * 4}],
        add_count=3,
        room=18,
    )
    assert "Return ONLY" in system
    assert "Do NOT return a full deck" in system
    assert "請新寫 3 張投影片" in user
    assert "[2] 來源：a.pdf" in user
    spoken, _ = build_generation_prompt(
        "庫", "口講用短頁", None,
        [{"filename": "a.pdf", "chunk_key": "c1", "content": "內容", "score": 0.9}],
        retrieval_failed=False,
    )
    assert "不要再加頁" in spoken
    assert "請新寫" not in spoken


@pytest.mark.asyncio
async def test_expand_undercovered_deck_adds_cited_pages() -> None:
    spec = _deck(12)
    chunks = [_chunk(i) for i in range(1, 7)]
    payload = {
        "slides": [
            {
                "title": "方法其實分兩路",
                "bullets": ["左路對影像 (參 [2])", "右路對熱訊 (參 [3])"],
                "speaker_notes": "這頁補來源裡還沒講的方法。",
                "layout_kind": "standard",
            },
            {
                "title": "誤差是在切機時出現",
                "bullets": ["切機批次掉 4 點 (參 [4])"],
                "speaker_notes": "數字在第四段。",
                "layout_kind": "standard",
            },
        ]
    }

    with patch(
        "app.services.studio_expand.call_llm_chat",
        new_callable=AsyncMock,
        return_value=json.dumps(payload, ensure_ascii=False),
    ):
        out = await expand_undercovered_deck(
            spec, chunks, "詳細簡報", bearer="t",
        )
    assert len(out.slides) == 14
    assert out.slides[0].title == spec.slides[0].title
    assert out.slides[-1].title == spec.slides[-1].title
    mid = [s.title for s in out.slides[1:-1]]
    assert "方法其實分兩路" in mid
    assert "誤差是在切機時出現" in mid


@pytest.mark.asyncio
async def test_expand_is_noop_for_spoken_even_if_sources_leftover() -> None:
    spec = _deck(5)
    chunks = [_chunk(i) for i in range(1, 9)]
    with patch(
        "app.services.studio_expand.call_llm_chat",
        new_callable=AsyncMock,
    ) as mocked:
        out = await expand_undercovered_deck(
            spec, chunks, "口講用短頁", bearer="t",
        )
    mocked.assert_not_called()
    assert len(out.slides) == 5
    assert [s.title for s in out.slides] == [s.title for s in spec.slides]
