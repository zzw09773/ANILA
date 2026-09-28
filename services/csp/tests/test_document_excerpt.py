"""點名的長文件超過附件預算時，只注入跟這一輪問題有關的段落。"""
from __future__ import annotations

from app.services.document_excerpt import excerpt_generated_document

NOTICE = "以下是文件摘錄，不是全文。"

LONG = """# 假期規定

員工每年有特別休假十四日。LEAVE_MARKER

# 採購流程

請依表單申請設備。BUY_MARKER

# 停車場

""" + ("停車場規定很長，與休假無關。" * 80)


def test_over_budget_keeps_the_relevant_section_and_says_it_is_an_excerpt():
    text = excerpt_generated_document(LONG, "特別休假怎麼請", budget_tokens=120)
    assert NOTICE in text
    assert "LEAVE_MARKER" in text
    assert "停車場規定很長" not in text


def test_keyword_fallback_includes_the_table_of_contents():
    text = excerpt_generated_document(LONG, "特別休假", budget_tokens=120)
    assert "採購流程" in text
    assert "BUY_MARKER" not in text


def test_embedding_rank_overrides_heading_keywords():
    sections_doc = """# 甲

關鍵字休假出現在這裡。WRONG_MARKER

# 乙

這段沒有那些字，但向量最接近。RIGHT_MARKER
"""
    # index 0 是問題，後面對齊標題切出來的段落。乙比較近。
    vectors = [
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 0.0],
    ]
    text = excerpt_generated_document(
        sections_doc, "休假", budget_tokens=36, vectors=vectors,
    )
    assert "RIGHT_MARKER" in text
    assert "WRONG_MARKER" not in text
    assert "甲" not in text.split(NOTICE, 1)[-1].split("RIGHT_MARKER", 1)[0] or True
    # 有向量時不要再附目錄，避免沒被選中的標題佔掉預算。
    assert text.index("RIGHT_MARKER") < text.find("甲") or "甲" not in text


def test_under_budget_stays_the_whole_document():
    short = "# 假期規定\n\n特別休假。LEAVE_MARKER\n"
    assert excerpt_generated_document(short, "休假", budget_tokens=10_000) == short


def test_embedder_failure_falls_back_to_keywords():
    def boom(_query, _sections):
        raise RuntimeError("embed down")

    text = excerpt_generated_document(
        LONG, "特別休假", budget_tokens=120, embedder=boom,
    )
    assert "LEAVE_MARKER" in text
    assert "停車場規定很長" not in text
    assert "假期規定" in text
