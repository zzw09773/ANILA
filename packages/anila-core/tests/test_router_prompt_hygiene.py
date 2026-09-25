"""Router 系統提示不得把內部細節交給模型，釐清只走 ASK。

不得外洩規則跟日期一樣，附在組好的提示後面，治理中心改三段提示刪不掉。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api import router_prompts as rp
from anila_core.api import router_server as rs
from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest

CSP_URL = f"{settings.csp_base_url}/v1/chat/completions"
CSP_AGENTS_URL = f"{settings.csp_base_url}/v1/agents"

_SRC = Path(rp.__file__).resolve().parent
_ENV_CALL_RE = re.compile(
    r"""(?:os\.environ(?:\.get)?|getenv)\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)
_ENV_BRACKET_RE = re.compile(
    r"""os\.environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""
)
_PATH_RE = re.compile(r"(?<![\w.+-])/[\w.+-]+(?:/[\w.+-]+)+")
_FILE_RE = re.compile(r"\b[\w.-]+\.(?:js|py)\b", re.IGNORECASE)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOST_RE = re.compile(
    r"\b(?:localhost|(?:[a-z0-9-]+\.)+(?:com|org|net|tw|io|dev|local|internal|lan))\b",
    re.IGNORECASE,
)
# 預覽改寫與舊網頁提示裡出現過的主機名稱。組好的系統提示不得再教模型這些字。
_HOST_TOKENS = ("cdnjs", "jsdelivr", "unpkg", "threejs.org", "cloudflare")


def _env_denylist() -> set[str]:
    """從組提示的程式裡把環境變數名稱掃出來，不另手寫一份。"""
    names: set[str] = set()
    for path in (
        _SRC / "router_server.py",
        _SRC / "router_prompts.py",
        _SRC.parent / "registry" / "remote_agent_manifest.py",
        _SRC.parent / "prompts" / "common_preamble.py",
    ):
        text = path.read_text(encoding="utf-8")
        names.update(_ENV_CALL_RE.findall(text))
        names.update(_ENV_BRACKET_RE.findall(text))
    assert names, "環境變數黑名單是空的，掃描沒掃到程式"
    return names


def _without_preview_hint(text: str) -> str:
    """預覽提示要寫出 Shell 會改寫的 script src，掃描其他洩漏時先拿掉這段。"""
    for hint in (rp.HTML_PREVIEW_HINT_ZH, rp.HTML_PREVIEW_HINT_EN):
        text = text.replace(hint, "")
    return text


def _assert_no_internal_leaks(text: str) -> None:
    assert rp.DISCLOSURE_RULE_ZH in text or rp.DISCLOSURE_RULE_EN in text
    assert "隔離內網" not in text
    assert "/anila/vendor/" not in text
    scanned = _without_preview_hint(text)
    assert _PATH_RE.search(scanned) is None, _PATH_RE.search(scanned)
    assert _FILE_RE.search(scanned) is None, _FILE_RE.search(scanned)
    assert _IP_RE.search(scanned) is None, _IP_RE.search(scanned)
    assert _HOST_RE.search(scanned) is None, _HOST_RE.search(scanned)
    lowered = scanned.lower()
    for token in _HOST_TOKENS:
        assert token not in lowered
    for name in _env_denylist():
        assert name not in text


def _dirty_agent() -> RemoteAgentManifest:
    return RemoteAgentManifest(
        agent_id="demo",
        name="示範助手",
        description_for_router=(
            "查規章。見 /anila/vendor/three/r128/three.min.js"
            " 與 ANILA_TRACE_TOKEN，主機 10.1.2.3。"
        ),
        endpoint_url="http://10.1.2.3:8080",
        capabilities={"tools": ["ANILA_TOKENIZE_URL"], "endpoint": "http://csp.internal"},
    )


def test_editable_defaults_do_not_contain_the_disclosure_rule():
    for text in (
        rp.DEFAULT_ROUTER_SYSTEM,
        rp.DEFAULT_PLAIN_ASSISTANT,
        rp.DEFAULT_FORCED_ANSWER,
    ):
        assert rp.DISCLOSURE_RULE_ZH not in text
        assert rp.DISCLOSURE_RULE_EN not in text
        assert "隔離內網" not in text
        assert "/anila/vendor/" not in text
        assert "three.min.js" not in text


def test_shipped_prompts_share_one_clarify_policy_and_drop_the_markdown_list():
    for text in (rp.DEFAULT_ROUTER_SYSTEM, rp.DEFAULT_PLAIN_ASSISTANT):
        assert "缺的資訊會讓答案實質不同時，才問一個問題" in text
        assert "盡快決定。一次只問一個問題。" in text
        assert "直接回答時寫明你採用的假設" in text
        assert "以 Markdown 項目清單列出候選" not in text
        assert "<AGENT_ID_1>" not in text
    zh = rp.clarify_policy(chinese=True, dispatch=True)
    en = rp.clarify_policy(chinese=False, dispatch=True)
    plain_zh = rp.clarify_policy(chinese=True, dispatch=False)
    assert "或派工" not in plain_zh
    assert "直接回答時寫明你採用的假設" in plain_zh
    assert "ASK:" in zh and "ASK:" in en
    assert "ASK*:" in zh and "ASK*:" in en
    assert "實質不同" in zh and "substantially" in en
    assert "一次只問一個問題" in zh and "at most one question" in en
    assert "假設" in zh and "assumption" in en.lower()
    assert "盡快決定" in zh and "Decide quickly" in en
    assert "Markdown" in zh and "Markdown" in en
    assert "系統提示" in rp.DISCLOSURE_RULE_ZH
    assert "內部路徑" in rp.DISCLOSURE_RULE_ZH
    assert "使用者層級" in rp.DISCLOSURE_RULE_ZH
    assert "system prompt" in rp.DISCLOSURE_RULE_EN.lower()
    assert "internal paths" in rp.DISCLOSURE_RULE_EN.lower()
    assert "user-level" in rp.DISCLOSURE_RULE_EN.lower()
    assert "下載" in rp.HTML_PREVIEW_HINT_ZH and "檔案放在哪裡" in rp.HTML_PREVIEW_HINT_ZH
    assert "download" in rp.HTML_PREVIEW_HINT_EN.lower()
    assert "where files are stored" in rp.HTML_PREVIEW_HINT_EN.lower()


def test_assembled_prompts_keep_the_rule_and_drop_internal_details():
    direct = rs._build_system_prompt([])
    dispatch = rs._build_system_prompt([_dirty_agent()])
    forced = rs._forced_answer_prompt()
    for text in (direct, dispatch, forced):
        _assert_no_internal_leaks(text)
        assert text.rstrip().endswith("Asia/Taipei。") or "Asia/Taipei." in text
    assert rp.DISCLOSURE_RULE_ZH in direct
    assert rp.DISCLOSURE_RULE_ZH in dispatch
    assert rp.DISCLOSURE_RULE_ZH in forced
    assert "查規章" in dispatch
    assert "示範助手" in dispatch
    assert "http://10.1.2.3" not in dispatch
    assert "csp.internal" not in dispatch
    assert "ANILA_TOKENIZE_URL" not in dispatch
    assert "【網頁】" in direct
    assert "【網頁】" not in forced
    assert "以 Markdown 項目清單列出候選" not in dispatch


def test_admin_edit_cannot_drop_the_rule_and_old_leaks_are_removed(monkeypatch):
    old_hint = (
        "【HTML 網頁】本系統在隔離內網。Three.js r128 與 OrbitControls 已放在"
        "同源 /anila/vendor/three/r128/three.min.js 與 OrbitControls.js。"
        "寫完整可執行的 HTML 時必須用這兩個路徑，禁止 cdnjs／jsDelivr／unpkg，"
        "也不要寫「請自行下載」的離線提醒。"
    )
    old_list = (
        "4. 若查詢有歧義——可能符合多個 agent，或意圖不清——不要猜測。\n"
        "你的問題可能跟這些方向有關：\n"
        "- <AGENT_ID_1>：<DESC_1>\n"
        "請問你想往哪個方向？\n"
    )
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            # 英文覆寫只夾路徑與設定名稱，語言仍是英文。
            rp.KEY_SYSTEM: (
                "Route the request.\n{agent_list}\n"
                "/anila/vendor/three/r128/three.min.js cdnjs ANILA_TRACE_ENDPOINT"
            ),
            rp.KEY_PLAIN: "Answer in English.\n/opt/anila/app.py unpkg ANILA_TOKENIZE_URL",
            rp.KEY_FORCED: (
                "請依院內規章回答。本系統部署於隔離內網，服務於測試。\n"
                + old_hint
                + "\n"
                + old_list
            ),
        },
    )
    plain = rs._build_system_prompt([])
    routed = rs._build_system_prompt([_dirty_agent()])
    forced = rs._forced_answer_prompt()
    for text in (plain, routed):
        _assert_no_internal_leaks(text)
        assert rp.DISCLOSURE_RULE_EN in text
        assert "今天是" not in text
        assert "請問你想往哪個方向" not in text
    _assert_no_internal_leaks(forced)
    assert rp.DISCLOSURE_RULE_ZH in forced
    assert "請問你想往哪個方向" not in forced
    stored = rs.current_router_prompts()
    assert rp.DISCLOSURE_RULE_EN not in stored[rp.KEY_PLAIN]
    assert rp.DISCLOSURE_RULE_ZH not in stored[rp.KEY_FORCED]
    # 英文覆寫自己沒寫 ASK 時，補上與中文相同的釐清政策。
    assert "at most one question" in plain
    assert "ASK*:" in plain


# 治理中心若存的是改版前的出廠全文，裡面本來就有 ASK:。
_OLD_ROUTER_2A = """2a. 若答案會因使用者沒說的一個事實而完全不同（哪一年、哪一份、哪個對象），
   且沒有 agent 該接手，不要猜。整段回覆的第一行就必須是 ASK: 開頭，
   前面不得有任何字元，格式為 ASK:<一個簡短問題>，選項可接在同一個
   問號後面、以 | 分隔、每個選項要短（ASK:要查哪一年？|2024|2025）。
   使用者可能要挑多個（問法像「哪幾個」「哪些」）時，改用 ASK*:，
   其餘格式相同（ASK*:要挑哪幾個？|甲|乙|丙）；只能選一個就用 ASK:。
   這會暫停並等使用者回答後才繼續；沒有這種缺口就直接回答或派工，
   不要為了確認而確認。ASK: 與 ASK*: 只在第一行有效，後文提到它不會暫停。"""

_OLD_RULE_4 = """4. 若查詢有歧義——可能符合多個 agent，或意圖不清——不要猜測。改以預設的繁體中文
  （台灣用語；使用者明確指定語言時依其指定）提出「一個」簡短釐清問題。以 Markdown 項目清單列出候選
   agent（最多三個），每個 agent 各佔一行，並以一個簡短問題作結。此路徑
   不得包含 DISPATCH 或任何假造的 agent id。

   輸出格式（下方的 <AGENT_ID_X> 與 <DESC_X> 僅為示意——請用上方
   "Available agents:" 清單中的真實 agent_id 與描述原文替換。絕不可把
   佔位符字串原樣複製進使用者可見的回覆。若 "Available agents:" 為
   "none"，不要走此路徑——改依規則 3 直接回答。）：

你的問題可能跟這些方向有關：

- <AGENT_ID_1>：<DESC_1>
- <AGENT_ID_2>：<DESC_2>

請問你想往哪個方向？"""

_OLD_PLAIN_2A = """2a. 只有當答案會因使用者沒說的一個事實而完全不同時才反問：整段回覆的第一行
   必須是 ASK:<一個簡短問題>，選項可接在問號後、以 | 分隔、每個選項要短
   （ASK:要查哪一年？|2024|2025）。使用者可能要挑多個（問法像「哪幾個」
   「哪些」）時改用 ASK*:，其餘格式相同（ASK*:要挑哪幾個？|甲|乙|丙）；
   只能選一個就用 ASK:。這會暫停並等使用者回答後才繼續；沒有這種缺口就
   直接回答，不要為了確認而確認。ASK: 與 ASK*: 只在第一行有效，後文提到
   它不會暫停。"""


def test_saved_old_default_clarify_rules_are_normalized_even_with_ask(monkeypatch):
    """舊出廠規則含 ASK:，不能因此當成新政策已在。"""
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "你是派工器。\n{agent_list}\n" + _OLD_ROUTER_2A + "\n" + _OLD_RULE_4,
            rp.KEY_PLAIN: "你是助理。\n" + _OLD_PLAIN_2A,
            rp.KEY_FORCED: "請直接回答。",
        },
    )
    routed = rs._build_system_prompt([_dirty_agent()])
    plain = rs._build_system_prompt([])
    assert "完全不同" not in routed
    assert "以 Markdown 項目清單列出候選" not in routed
    assert "請問你想往哪個方向" not in routed
    assert "一次只問一個問題" in routed
    assert "直接回答時寫明你採用的假設" in routed
    assert "完全不同" not in plain
    assert "一次只問一個問題" in plain
    assert "或派工" not in plain


def test_custom_admin_clarify_text_is_not_rewritten(monkeypatch):
    custom = "自訂派工。\n{agent_list}\n不清楚時先 ASK:自訂問題，再視情況條列。"
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: custom,
            rp.KEY_PLAIN: "自訂助理。遇到缺口就 ASK:我的問法。",
            rp.KEY_FORCED: "請直接回答。",
        },
    )
    routed = rs._build_system_prompt([_dirty_agent()])
    plain = rs._build_system_prompt([])
    assert "ASK:自訂問題" in routed
    assert "缺的資訊會讓答案實質不同" not in routed
    assert "ASK:我的問法" in plain
    assert "缺的資訊會讓答案實質不同" not in plain


def test_assumptions_apply_only_to_direct_answers_not_dispatch():
    router = rp.DEFAULT_ROUTER_SYSTEM
    plain = rp.DEFAULT_PLAIN_ASSISTANT
    assert "否則直接回答或派工，並寫明你採用的假設" not in router
    assert "直接回答時寫明你採用的假設" in router
    assert "查詢維持使用者原文" in router
    assert "不要把假設寫進那一行" in router
    assert "或派工" not in plain
    assert "直接回答時寫明你採用的假設" in plain


def test_plain_override_without_ask_does_not_mention_dispatch(monkeypatch):
    monkeypatch.setattr(
        rs,
        "current_router_prompts",
        lambda: {
            rp.KEY_SYSTEM: "Route.\n{agent_list}",
            rp.KEY_PLAIN: "請用繁體中文回答。",
            rp.KEY_FORCED: "請直接回答。",
        },
    )
    plain = rs._build_system_prompt([])
    assert "一次只問一個問題" in plain
    assert "否則直接回答" in plain
    assert "或派工" not in plain
    assert "DISPATCH" not in plain


def test_preview_hint_names_the_script_form_the_shell_localizes():
    for hint in (rp.HTML_PREVIEW_HINT_ZH, rp.HTML_PREVIEW_HINT_EN):
        assert "cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js" in hint
        assert "cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js" in hint
        assert "three.module.js" in hint
        assert "esm.sh" in hint
        assert "/anila/vendor/" not in hint
        assert "隔離內網" not in hint
    assembled = rs._build_system_prompt([])
    assert "cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js" in assembled
    assert "/anila/vendor/" not in assembled


def test_user_visible_agent_notices_hide_internal_ids():
    for agent_id in ("anila-studio", "agent-a", "csp.internal"):
        notice = rs._unregistered_agent_notice(agent_id)
        outage = rs._agent_outage_message(agent_id)
        assert agent_id not in notice
        assert agent_id not in outage
    assert "法規助手" in rs._agent_outage_message("法規助手")
    assert "法規助手" in rs._unregistered_agent_notice("法規助手")


def test_middle_dot_list_is_ordinary_prose_and_directives_still_parse():
    raw = "你的問題可能跟這些方向有關： · 甲：說明 · 乙：說明 請問你想往哪個方向？"
    assert rs._parse_ask(raw) is None
    assert rs._parse_dispatch(raw) is None
    assert rs._strip_ask_syntax(raw) == raw
    ask = rs._parse_ask("ASK*:要挑哪幾個？|甲|乙|丙")
    assert ask is not None and ask["multi"] is True
    assert [item["label"] for item in ask["options"]] == ["甲", "乙", "丙"]
    dispatched = rs._parse_dispatch("DISPATCH:demo:做一份報告")
    assert dispatched is not None and dispatched[0] == "demo"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-hygiene.db"
    yield db
    await close_all_connections()


def _llm(content: str, finish: str = "stop", reasoning: str = "") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-hygiene",
        "object": "chat.completion",
        "model": "router-llm",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def _system_text(payload: dict) -> str:
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def _post(client: TestClient, **extra):
    headers = {"Authorization": "Bearer sk-test"}
    headers.update(extra.pop("headers", {}))
    body = {"messages": [{"role": "user", "content": "幫我做一份報告"}], "stream": False}
    body.update(extra)
    return client.post("/v1/chat/completions", json=body, headers=headers)


def _empty_agents() -> httpx.Response:
    return httpx.Response(200, json={"data": []})


def _demo_agents() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "demo",
                    "name": "示範助手",
                    "description_for_router": "查規章。見 /opt/anila/app.py 與 http://10.9.8.7/status",
                    "endpoint_url": "http://10.9.8.7:9000",
                    "capabilities": {"endpoint": "http://csp.internal/hooks"},
                    "requires_encryption": False,
                }
            ]
        },
    )


@respx.mock
def test_direct_answer_system_prompt_hides_internals(db_path: Path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=_llm("直接回答。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_empty_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = _post(client, session_id="s-direct")
    assert resp.status_code == 200, resp.text
    _assert_no_internal_leaks(_system_text(seen[0]))


@respx.mock
def test_dispatch_decision_system_prompt_hides_internals(db_path: Path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        if len(seen) == 1:
            return httpx.Response(200, json=_llm("DISPATCH:demo:幫我做一份報告"))
        return httpx.Response(200, json=_llm("agent 已處理。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_demo_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    resp = _post(client, session_id="s-dispatch")
    assert resp.status_code == 200, resp.text
    system = _system_text(seen[0])
    _assert_no_internal_leaks(system)
    assert "DISPATCH:" in system
    assert "查規章" in system
    assert "10.9.8.7" not in system
    assert "csp.internal" not in system
    assert "app.py" not in system


@respx.mock
def test_ask_resume_and_rescue_system_prompts_hide_internals(
    db_path: Path,
) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        n = len(seen)
        if n == 1:
            return httpx.Response(200, json=_llm("ASK:要哪一種？|技術評估|市場"))
        if n == 2:
            return httpx.Response(200, json=_llm(" \n ", "length", "想過題目"))
        return httpx.Response(200, json=_llm("救援後的正文。"))

    respx.get(CSP_AGENTS_URL).mock(return_value=_empty_agents())
    respx.post(CSP_URL).mock(side_effect=handler)
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    asked = _post(client, session_id="s-ask")
    assert asked.status_code == 200, asked.text
    state = client.get(
        "/v1/sessions/s-ask/state",
        headers={"Authorization": "Bearer sk-test"},
    )
    interrupt_id = state.json()["pending_interrupts"][0]["id"]
    resumed = client.post(
        "/v1/sessions/s-ask/answer",
        json={
            "interrupt_id": interrupt_id,
            "answer": {"selected": ["技術評估"], "other_text": ""},
            "stream": False,
        },
        headers={"Authorization": "Bearer sk-test"},
    )
    assert resumed.status_code == 200, resumed.text
    assert len(seen) >= 3
    # 1 直答／反問，2 ASK 續答，3 救援。救援沿用同一則系統提示。
    for payload in seen:
        _assert_no_internal_leaks(_system_text(payload))
