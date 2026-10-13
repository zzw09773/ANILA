"""Programmatic article diff. Synthetic club rules only — not a real regulation."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

import difflib

from anila_core.diff.article_diff import (
    attach_model_output,
    compare_docx_bytes,
    compare_texts,
    explanation_request,
    parse_article_number,
)

# The module object is needed so the difflib spy patches the name the
# comparer actually calls. Importing the functions above does not expose it.
import anila_core.diff.article_diff as article_diff_module


def test_chinese_and_arabic_article_numbers():
    assert parse_article_number("一") == 1
    assert parse_article_number("十") == 10
    assert parse_article_number("十二") == 12
    assert parse_article_number("二十") == 20
    assert parse_article_number("二十一") == 21
    assert parse_article_number("一百") == 100
    assert parse_article_number("一百零五") == 105
    assert parse_article_number("一百一十") == 110
    assert parse_article_number("１２") == 12
    assert parse_article_number("0") is None
    assert parse_article_number("零") is None


def test_aligns_by_article_number_not_position():
    old = "第一條\n圖書館週一休息。\n第二條\n茶會在週三。"
    new = "第二條\n茶會在週三。\n第一條\n圖書館週一休息。"
    result = compare_texts(old, new)
    assert result["parse_complete"] is True
    assert result["changes"] == []
    assert result["summary"] == "依條號比對，沒有找到差異。"


def test_modified_article_keeps_full_original_text():
    old = "第一條\n圖書館週一休息。\n第二條\n茶會在週三。自備杯子。"
    new = "第一條\n圖書館週一休息。\n第二條\n茶會在週四。自備杯子。"
    result = compare_texts(old, new)
    assert result["parse_complete"] is True
    assert [change["id"] for change in result["changes"]] == ["diff:2:modified"]
    change = result["changes"][0]
    assert change["importance"] == "high"
    assert change["article"] == "第二條"
    assert "茶會在週三。自備杯子。" in change["before"]
    assert "茶會在週四。自備杯子。" in change["after"]
    assert change["before"].startswith("第二條")


def test_sub_article_and_added_removed():
    old = "第三條\n總則適用。\n第三條之一\n雨天取消。\n第四條\n座位先到先坐。"
    new = "第三條\n總則適用。\n第三條之一\n雨天改至室內。\n第五條\n自備杯子。"
    result = compare_texts(old, new)
    assert [change["id"] for change in result["changes"]] == [
        "diff:3-1:modified",
        "diff:4:removed",
        "diff:5:added",
    ]
    modified = result["changes"][0]
    assert "雨天取消。" in modified["before"]
    assert "雨天改至室內。" in modified["after"]
    assert result["changes"][2]["after"].endswith("自備杯子。")


def test_whitespace_only_change_is_low_importance():
    old = "第一條\n茶會在週三。"
    new = "第一條\n茶會在  週三。"
    change = compare_texts(old, new)["changes"][0]
    assert change["importance"] == "low"
    assert change["id"] == "diff:1:modified"


def test_preamble_change_is_its_own_id():
    old = "園遊會須知\n第一條\n攤位限一個。"
    new = "園遊會說明\n第一條\n攤位限一個。"
    result = compare_texts(old, new)
    assert result["changes"][0]["id"] == "diff:preamble:modified"
    assert "園遊會須知" in result["changes"][0]["before"]
    assert result["changes"][0]["importance"] == "high"


def test_unparsed_text_does_not_claim_there_are_no_other_differences():
    result = compare_texts("今天茶會改到週四。", "今天茶會仍在週三。")
    assert result["parse_complete"] is False
    assert result["changes"] == []
    assert "沒有找到差異" not in result["summary"]
    assert "不能據此" in result["summary"]
    assert any("不能據此" in warning for warning in result["warnings"])


def test_one_sided_articles_are_not_reported_as_a_complete_add_list():
    result = compare_texts("第一條\n圖書館週一休息。", "這段沒有條號，只寫茶會改期。")
    assert result["changes"] == []
    assert result["parse_complete"] is False
    assert "沒有找到差異" not in result["summary"]


def test_duplicate_article_numbers_are_not_aligned():
    result = compare_texts("第一條\n甲方案。\n第一條\n乙方案。", "第一條\n甲方案。")
    assert result["changes"] == []
    assert result["parse_complete"] is False
    assert "條號重複" in result["warnings"][0]


def test_diff_calls_difflib():
    calls = {"n": 0}
    real = difflib.SequenceMatcher

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    article_diff_module.difflib.SequenceMatcher = wrapped
    try:
        compare_texts("第一條\n茶會在週三。", "第一條\n茶會在週四。")
    finally:
        article_diff_module.difflib.SequenceMatcher = real
    assert calls["n"] >= 1


def test_explanation_prompt_lists_only_changed_ids():
    old = "第一條\n圖書館週一休息。UNCHANGED_MARK\n第二條\n茶會在週三。"
    new = "第一條\n圖書館週一休息。UNCHANGED_MARK\n第二條\n茶會在週四。"
    diff = compare_texts(old, new)
    request = explanation_request(diff)
    blob = request["messages"][1]["content"]
    assert request["allowed_ids"] == ["diff:2:modified"]
    assert "UNCHANGED_MARK" not in blob
    assert "茶會在週三" in blob
    assert "不得新增" in request["messages"][0]["content"]
    assert "parse_complete: true" in blob


def test_model_output_drops_invented_ids_and_invented_articles():
    diff = compare_texts("第一條\n茶會在週三。", "第一條\n茶會在週四。")
    request = explanation_request(diff)
    raw = """
    說明如下：
    [
      {"id": "diff:9:added", "text": "第九條是新的。"},
      {"id": "diff:1:modified", "text": "茶會從週三改到週四。"},
      {"id": "diff:1:modified", "text": "第五條也改為週末。"}
    ]
    """
    # The third item repeats a real id but claims another article changed.
    # First occurrence of the real id is kept; the invented id is not.
    attached = attach_model_output(
        diff,
        raw,
        allowed_ids=request["allowed_ids"],
        allowed_article_keys=request["allowed_article_keys"],
        prompt_truncated=False,
    )
    assert [item["id"] for item in attached["explanations"]] == ["diff:1:modified"]
    assert attached["explanations"][0]["text"] == "茶會從週三改到週四。"
    assert attached["changes"] == diff["changes"]
    assert attached["model_status"] == "filtered"
    assert "沒有顯示" in attached["model_note"]
    assert not any(item["id"] == "diff:9:added" for item in attached["changes"])
    invented_article = '[{"id":"diff:1:modified","text":"第五條也改為週末。"}]'
    rejected = attach_model_output(
        diff,
        invented_article,
        allowed_ids=request["allowed_ids"],
        allowed_article_keys=request["allowed_article_keys"],
        prompt_truncated=False,
    )
    assert rejected["explanations"] == []
    assert rejected["changes"] == diff["changes"]


def test_incomplete_parse_rejects_a_completeness_claim():
    diff = compare_texts("沒有條號的通知。", "還是沒有條號。")
    request = explanation_request(diff)
    assert request["allowed_ids"] == []
    assert "parse_complete: false" in request["messages"][1]["content"]
    raw = '[{"id":"diff:1:added","text":"已完整比對，沒有其他差異。"}]'
    attached = attach_model_output(
        diff,
        raw,
        allowed_ids=["diff:1:added"],
        allowed_article_keys=["1"],
        prompt_truncated=False,
    )
    assert attached["explanations"] == []
    assert attached["changes"] == []
    assert "沒有找到差異" not in attached["summary"]


def test_negated_completeness_sentence_can_stay_when_the_id_is_real():
    diff = compare_docx_bytes(
        _docx(["第一條", "茶會在週三。"], extra_body=_sdt("隱藏句子。")),
        _docx(["第一條", "茶會在週四。"]),
    )
    assert diff["parse_complete"] is False
    assert diff["changes"][0]["id"] == "diff:1:modified"
    request = explanation_request(diff)
    assert "parse_complete: false" in request["messages"][1]["content"]
    raw = '[{"id":"diff:1:modified","text":"茶會改期。不能據此說沒有其他差異。"}]'
    attached = attach_model_output(
        diff,
        raw,
        allowed_ids=request["allowed_ids"],
        allowed_article_keys=request["allowed_article_keys"],
        prompt_truncated=False,
    )
    assert attached["explanations"][0]["text"].startswith("茶會改期")


def test_docx_round_trip_and_unreadable_file():
    before = _docx(["第一條", "圖書館週一休息。", "第二條", "座位先到先坐。"])
    after = _docx(["第一條", "圖書館週一休息。", "第二條", "座位需登記。"])
    result = compare_docx_bytes(before, after)
    assert result["readable"] is True
    assert result["parse_complete"] is True
    assert result["changes"][0]["id"] == "diff:2:modified"
    assert "座位先到先坐。" in result["changes"][0]["before"]

    broken = compare_docx_bytes(b"not-a-zip", after)
    assert broken["readable"] is False
    assert broken["changes"] == []
    assert "沒有找到差異" not in broken["summary"]
    assert "不能據此" in broken["summary"]


def test_identical_articles_inside_a_content_control_are_not_called_complete():
    before = _docx(["第一條", "攤位限一個。"], extra_body=_sdt("另有未展開的句子。"))
    after = _docx(["第一條", "攤位限一個。"])
    result = compare_docx_bytes(before, after)
    assert result["changes"] == []
    assert result["parse_complete"] is False
    assert "沒有找到差異" not in result["summary"]
    assert "不能據此" in result["summary"]


def test_table_cell_is_part_of_the_article():
    before = _docx([], extra_body=_table(["第一條", "座位先到先坐。"]))
    after = _docx([], extra_body=_table(["第一條", "座位需登記。"]))
    result = compare_docx_bytes(before, after)
    assert result["parse_complete"] is True
    assert "座位先到先坐。" in result["changes"][0]["before"]
    assert "座位需登記。" in result["changes"][0]["after"]


def test_tracked_deletion_blocks_a_no_difference_claim():
    body = (
        "<w:p>"
        "<w:r><w:t>第一條</w:t></w:r>"
        "</w:p>"
        "<w:p>"
        "<w:r><w:t>茶會在週三。</w:t></w:r>"
        "<w:del><w:r><w:delText>舊時段。</w:delText></w:r></w:del>"
        "</w:p>"
    )
    same = _docx(["第一條", "茶會在週三。"])
    result = compare_docx_bytes(_docx_raw(body), same)
    assert result["changes"] == []
    assert result["parse_complete"] is False
    assert "沒有找到差異" not in result["summary"]


def _docx(paragraphs: list[str], extra_body: str = "") -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    chunks = []
    for text in paragraphs:
        chunks.append(
            f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>'
        + "".join(chunks)
        + extra_body
        + "<w:sectPr/></w:body></w:document>"
    )
    return _pack(document)


def _docx_raw(body_xml: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body_xml}<w:sectPr/></w:body></w:document>'
    )
    return _pack(document)


def _pack(document: str) -> bytes:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", rels)
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def _sdt(text: str) -> str:
    return (
        "<w:sdt><w:sdtContent><w:p><w:r><w:t>"
        + escape(text)
        + "</w:t></w:r></w:p></w:sdtContent></w:sdt>"
    )


def _table(cell_texts: list[str]) -> str:
    cells = "".join(
        "<w:tc><w:p><w:r><w:t>" + escape(text) + "</w:t></w:r></w:p></w:tc>"
        for text in cell_texts
    )
    return f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"
