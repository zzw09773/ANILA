"""Router system-prompt 繁中化＋共同前導接線的守護測試。

只驗提示詞字串組裝（.format 安全、前導／語言／DISPATCH 契約），
不碰路由控制流（OWNER-QUESTIONS Q7 凍結）。
"""

from anila_core.api.router_prompts import INTRANET_HTML_HINT
from anila_core.api.router_server import (
    _PLAIN_ASSISTANT_TEMPLATE,
    _RECOMPOSE_SYSTEM_PROMPT,
    _ROUTER_SYSTEM_TEMPLATE,
)
from anila_core.prompts import COMMON_PREAMBLE, IDENTITY

# 與 test_common_preamble 同集：無歧義純簡體字形。
_SIMPLIFIED_ONLY = set(
    "国湾视频质软网络东车贝页风飞专业书买亚产亲亿传体优备关兴写军农况净"
    "则创删动劳区医华协单卖厂历压县参双变发叶号后员响团园围图圆场坏块坚"
    "声处复头夹夺妇学宁宝实审宫层岁岛币师带帮广庆库应张弯强归当录忆态总"
    "恶惊愿战户报担拟拥挂损换据败货贴费资赛简体条来对时说话让证权满线"
)


def test_router_templates_format_safe_and_localized():
    router = _ROUTER_SYSTEM_TEMPLATE.format(
        agent_list="Available agents:\n  - dummy-agent: dummy desc"
    )
    plain = _PLAIN_ASSISTANT_TEMPLATE.format()
    recompose = _RECOMPOSE_SYSTEM_PROMPT.format()

    assert router.startswith(COMMON_PREAMBLE)
    assert "DISPATCH:" in router
    assert "繁體中文" in router
    assert "【平台身分】" in router
    assert "{agent_list}" not in router
    assert "第一個字元" in router
    assert "DISPATCH:" in router

    assert plain.startswith(COMMON_PREAMBLE)
    assert "目前沒有已註冊的 agent" in plain
    assert "繁體中文" in plain
    assert "【平台身分】" in plain
    assert "第一個字元" in plain
    assert "DISPATCH:" not in plain

    assert recompose.startswith(IDENTITY)
    assert "【平台身分】" in recompose
    # 改寫層只用身分＋專用語言階梯，不含完整前導，也不含「最高優先」。
    assert COMMON_PREAMBLE not in recompose
    assert "最高優先" not in recompose
    assert "【輸出語言規則】" in recompose
    assert recompose.count("【輸出語言規則】") == 1
    assert "①" in recompose and "②" in recompose and "③" in recompose
    # 舊句「維持原回覆語言，除非…」已由階梯取代，不得殘留。
    assert "維持原回覆語言，除非" not in recompose


def test_direct_answer_rule_forbids_agent_scope_bleed_and_states_positive_range():
    router = _ROUTER_SYSTEM_TEMPLATE
    plain = _PLAIN_ASSISTANT_TEMPLATE
    assert "直接回答時不得提及任何 agent 的名稱、專責領域、模組或服務範圍" in router
    assert "本系統／本平台僅提供…" in router
    assert "不在服務範圍" in router
    assert "無法就…進一步協助" in router
    assert "agent 清單只用來決定是否派工，不是你的能力邊界。" in router
    assert "ANILA 直接回答的範圍包含院內人員的一般研究、技術與文件問題" in router
    assert "解讀使用者附上的檔案" in router
    assert "只要你能回答，就直接回答。" in router
    assert "ANILA 直接回答的範圍包含院內人員的一般研究、技術與文件問題" in plain
    assert "只要你能回答，就直接回答。" in plain
    assert "只要你能回答，就直接回答。" not in COMMON_PREAMBLE
    assert "直接回答時不得提及任何 agent 的名稱" not in COMMON_PREAMBLE
    assert "不是你的能力邊界" not in COMMON_PREAMBLE


def test_router_prompt_task_text_has_no_simplified_characters():
    # 前導本身另有守護；這裡釘任務段＋組合結果也不引入簡體。
    for text in (
        _ROUTER_SYSTEM_TEMPLATE,
        _PLAIN_ASSISTANT_TEMPLATE,
        _RECOMPOSE_SYSTEM_PROMPT,
        INTRANET_HTML_HINT,
    ):
        hits = sorted({ch for ch in text if ch in _SIMPLIFIED_ONLY})
        assert not hits, f"Router prompt 含簡體字：{hits}"
