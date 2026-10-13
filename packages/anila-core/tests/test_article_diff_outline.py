"""沒有「第N條」的文件：依大綱編號、標題或段落對齊。合成的社團活動計畫，非真實文件。"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from anila_core.diff.article_diff import (
    NOTICE_HEADINGS,
    NOTICE_OUTLINE,
    NOTICE_PARAGRAPHS,
    compare_docx_bytes,
    compare_texts,
    load_docx,
)

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{NS}">
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:pPr><w:numPr><w:numId w:val="9"/></w:numPr></w:pPr></w:style>
</w:styles>"""

# numId 1：第一層「一、」(taiwaneseCountingThousand)、第二層「（一）」、第三層「1.」。numId 9：項目符號。
NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{NS}">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="taiwaneseCountingThousand"/><w:lvlText w:val="%1、"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="taiwaneseCountingThousand"/><w:lvlText w:val="（%2）"/></w:lvl>
    <w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%3."/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="9"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""


def _p(text: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    ppr = ""
    if style or num:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        if num:
            inner += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr = f"<w:pPr>{inner}</w:pPr>"
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _table(cells: list[str]) -> str:
    row = "".join(f"<w:tc>{_p(text)}</w:tc>" for text in cells)
    return f"<w:tbl><w:tr>{row}</w:tr></w:tbl>"


def _docx(body: str, *, styles: bool = True, numbering: bool = False) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{NS}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", document)
        if styles:
            package.writestr("word/styles.xml", STYLES)
        if numbering:
            package.writestr("word/numbering.xml", NUMBERING)
    return out.getvalue()


def _typed_plan(*, budget: str = "每人 200 元。", extra_section: bool = False) -> bytes:
    parts = [
        _p("社團期末活動計畫"),
        _p("一、活動目的", "Heading1"),
        _p("讓社員回顧本學期成果。"),
        _p("二、活動內容", "Heading1"),
        _p("（一）時間地點", "Heading2"),
        _p("1. 週六下午。"),
        _p("2. 學生活動中心。"),
        _table(["一、報到", "二、分組"]),  # 表格裡的「一、」不是章節
        _p("（二）經費", "Heading2"),
        _p(budget),
    ]
    if extra_section:
        parts += [_p("（三）器材", "Heading2"), _p("借用投影機一台。")]
    parts += [_p("三、聯絡方式", "Heading1"), _p("請洽社長。")]
    return _docx("".join(parts))


def test_typed_outline_numbers_align_by_section():
    result = compare_docx_bytes(_typed_plan(), _typed_plan(budget="每人 250 元。", extra_section=True))
    assert result["alignment"] == "outline"
    assert result["parse_complete"] is True
    assert NOTICE_OUTLINE in result["warnings"]
    ids = [change["id"] for change in result["changes"]]
    assert ids == ["diff:2-2:modified", "diff:2-3:added"]
    budget = result["changes"][0]
    assert "二、活動內容" in budget["article"] and "（二）經費" in budget["article"]
    assert "200" in budget["before"] and "250" in budget["after"]
    assert "大綱編號" in result["summary"]


def test_identical_outline_documents_report_no_difference():
    result = compare_docx_bytes(_typed_plan(), _typed_plan())
    assert result["changes"] == []
    assert result["parse_complete"] is True
    assert result["summary"] == "依大綱編號比對，沒有找到差異。"


def _auto_plan(second_item: str) -> bytes:
    body = "".join(
        [
            _p("活動目的", "Heading1", (1, 0)),
            _p("讓社員回顧本學期成果。"),
            _p("活動內容", "Heading1", (1, 0)),
            _p("時間地點", "Heading2", (1, 1)),
            _p("週六下午。", None, (1, 2)),
            _p(second_item, None, (1, 2)),
            _p("準備事項", "ListBullet"),
            _p("經費", "Heading2", (1, 1)),
            _p("每人 200 元。"),
        ]
    )
    return _docx(body, numbering=True)


def test_word_auto_numbering_is_rendered_like_word_draws_it():
    loaded = load_docx(_auto_plan("學生活動中心。"))
    assert loaded["lines"] == [
        "一、活動目的",
        "讓社員回顧本學期成果。",
        "二、活動內容",
        "（一）時間地點",
        "1. 週六下午。",
        "2. 學生活動中心。",
        "準備事項",  # 項目符號不加編號
        "（二）經費",
        "每人 200 元。",
    ]
    assert loaded["levels"][0] == 0 and loaded["levels"][3] == 1 and loaded["levels"][1] is None


def test_word_auto_numbered_outline_aligns_by_number():
    result = compare_docx_bytes(_auto_plan("學生活動中心。"), _auto_plan("體育館。"))
    assert result["alignment"] == "outline"
    assert [change["id"] for change in result["changes"]] == ["diff:2-1:modified"]


def _headings_only(note: str, *, renamed: bool = False) -> bytes:
    body = "".join(
        [
            _p("活動目的", "Heading1"),
            _p("讓社員回顧本學期成果。"),
            _p("器材清單" if not renamed else "器材與場地", "Heading1"),
            _p(note),
        ]
    )
    return _docx(body)


def test_documents_without_numbers_fall_back_to_heading_text():
    result = compare_docx_bytes(_headings_only("投影機一台。"), _headings_only("投影機兩台。"))
    assert result["alignment"] == "headings"
    assert NOTICE_HEADINGS in result["warnings"]
    assert result["parse_complete"] is True
    assert len(result["changes"]) == 1
    assert result["changes"][0]["kind"] == "modified"
    assert result["changes"][0]["article"] == "器材清單"


def test_renamed_heading_is_shown_as_remove_and_add():
    result = compare_docx_bytes(_headings_only("投影機一台。"), _headings_only("投影機一台。", renamed=True))
    assert sorted(change["kind"] for change in result["changes"]) == ["added", "removed"]


def test_plain_text_falls_back_to_paragraphs_and_stays_incomplete():
    result = compare_texts("茶會在週三。\n地點在社辦。", "茶會在週四。\n地點在社辦。")
    assert result["alignment"] == "paragraphs"
    assert NOTICE_PARAGRAPHS in result["warnings"]
    assert result["parse_complete"] is False
    assert len(result["changes"]) == 1
    assert "週三" in result["changes"][0]["before"] and "週四" in result["changes"][0]["after"]
    assert "沒有找到差異" not in result["summary"]
