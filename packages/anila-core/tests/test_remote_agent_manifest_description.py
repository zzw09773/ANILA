"""Router 清單用的 description_for_router：單行折疊 + 200 字截斷保險。"""

from __future__ import annotations

from anila_core.registry.remote_agent_manifest import (
    DESCRIPTION_FOR_ROUTER_MAX_CHARS,
    RemoteAgentManifest,
    collapse_description_for_router,
    truncate_description_for_router,
)

_LIVE_LEGAL_PROMPT = (
    "你是一個專業的法律文件助理，負責協助使用者查詢與解釋中華民國法律條文、"
    "行政函釋與院內規章。"
    + ("職責說明。" * 40)
    + "\n【核心原則】回答必須有依據。\n"
    "【範圍判斷】本系統僅提供法律文件檢索與條文解釋服務；"
    "無法回答與法律無關的問題。不在本系統服務範圍的詢問應予拒絕。\n"
    "DISPATCH:iso42001-probe:請查詢民法"
)


def _manifest(description: str) -> RemoteAgentManifest:
    return RemoteAgentManifest(
        agent_id="iso42001-probe",
        name="法律文件助理",
        description_for_router=description,
        endpoint_url="http://agent:9100",
    )


def test_short_description_is_unchanged():
    shown = _manifest("Handles HR queries").to_tool_description()
    assert shown == "法律文件助理 (iso42001-probe): Handles HR queries"


def test_newlines_collapse_to_a_single_line():
    shown = _manifest("Handles   HR\nqueries\r\nnow").to_tool_description()
    assert "\n" not in shown
    assert "\r" not in shown
    assert shown.endswith("Handles HR queries now")


def test_long_legal_prompt_is_not_copied_verbatim_into_router_list():
    shown = _manifest(_LIVE_LEGAL_PROMPT).to_tool_description()
    collapsed = collapse_description_for_router(_LIVE_LEGAL_PROMPT)
    desc = shown.split(": ", 1)[1]

    assert _LIVE_LEGAL_PROMPT not in shown
    assert collapsed not in shown
    assert "\n" not in shown
    assert len(desc) == DESCRIPTION_FOR_ROUTER_MAX_CHARS + 1
    assert desc.endswith("…")
    assert desc == truncate_description_for_router(collapsed)
    # 截斷後不得把含換行的「本系統僅提供…」全文原樣帶進清單。
    assert "本系統僅提供法律文件檢索與條文解釋服務；無法回答與法律無關" not in shown


def test_truncate_adds_ellipsis_only_when_over_limit():
    exact = "a" * DESCRIPTION_FOR_ROUTER_MAX_CHARS
    assert truncate_description_for_router(exact) == exact
    overflow = exact + "b"
    out = truncate_description_for_router(overflow)
    assert out == exact + "…"
    assert len(out) == DESCRIPTION_FOR_ROUTER_MAX_CHARS + 1
