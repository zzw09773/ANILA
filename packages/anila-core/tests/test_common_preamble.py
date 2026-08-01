"""共同前導 SSOT 的守護測試。

守的不變式：
1. 五段齊全且依序出現在 COMMON_PREAMBLE。
2. 輕量版只含身分＋語言，不含國家語境／紀年／資料紀律（省 token 的設計意圖）。
3. 前導本文零簡體字（含禁用語清單本身也必須用繁體寫）。
4. 紀年公式自洽（114＋1911＝2025 同時出現在文本裡）。
5. 長度天花板：完整前導 ≤ 1200 字元——prefix cache 與 reasoning 燒 token
   的成本都跟前導長度掛鉤（活體實測見設計文件 §9），不許無感膨脹。
"""

from anila_core.prompts import (
    COMMON_PREAMBLE,
    DATA_DISCIPLINE,
    ERA_RULES,
    IDENTITY,
    LANGUAGE_PREAMBLE,
    LANGUAGE_RULES,
    NATIONAL_TERMINOLOGY,
    compose,
)

# 無歧義「純簡體」字元集（繁體文本中不該出現的字形；共用字不在內）。
_SIMPLIFIED_ONLY = set(
    "国湾视频质软网络东车贝页风飞专业书买亚产亲亿传体优备关兴写军农况净"
    "则创删动劳区医华协单卖厂历压县参双变发叶号后员响团园围图圆场坏块坚"
    "声处复头夹夺妇学宁宝实审宫层岁岛币师带帮广庆库应张弯强归当录忆态总"
    "恶惊愿战户报担拟拥挂损换据败货贴费资赛简体条来对时说话让证权满线"
)

_ALL_SECTIONS = (IDENTITY, LANGUAGE_RULES, NATIONAL_TERMINOLOGY, ERA_RULES, DATA_DISCIPLINE)


def test_full_preamble_contains_all_sections_in_order():
    positions = [COMMON_PREAMBLE.find(s) for s in _ALL_SECTIONS]
    assert all(p >= 0 for p in positions), "有段落沒進完整前導"
    assert positions == sorted(positions), "段落順序跑掉了"


def test_light_preamble_is_identity_plus_language_only():
    assert IDENTITY in LANGUAGE_PREAMBLE
    assert LANGUAGE_RULES in LANGUAGE_PREAMBLE
    for heavy in (NATIONAL_TERMINOLOGY, ERA_RULES, DATA_DISCIPLINE):
        assert heavy not in LANGUAGE_PREAMBLE, "輕量版混進了重段落"


def test_no_simplified_characters_anywhere():
    hits = sorted({ch for ch in COMMON_PREAMBLE if ch in _SIMPLIFIED_ONLY})
    assert not hits, f"前導含簡體字：{hits}"


def test_era_formula_is_self_consistent():
    # 文本宣稱：民國114年＝西元2025年，公式＝西元−1911。
    assert "1911" in ERA_RULES and "114" in ERA_RULES and "2025" in ERA_RULES
    assert 114 + 1911 == 2025


def test_full_preamble_length_budget():
    assert len(COMMON_PREAMBLE) <= 1200, (
        f"完整前導 {len(COMMON_PREAMBLE)} 字元，超出 1200 天花板——"
        "要加內容先讀設計文件 §9 的成本量測再說"
    )


def test_compose_strips_and_joins():
    assert compose(" a ", "", "b") == "a\n\nb"
