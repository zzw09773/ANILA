# -*- coding: utf-8 -*-
"""院內規章注入與五狀態 payload —— 「以為有依據，其實沒有」的防線。

這一支守的是 Task 6 的四條命：

1. **狀態一定明帶。** ``anila_meta.kb_state`` 必須出現在**四個** chat 出口上
   （agent SSE / agent 非串流 / model SSE / model 非串流）。缺席**不等於**
   ``not_searched``：一旦「沒有這個欄位就當作沒查過」，渲染管線壞掉的那天
   所有答案都會靜默降級成「沒查過」，而使用者看不出差別。
2. **無 header ＝ 完全不檢索。** 派工那通（agent-facing）與 ANILALM 不帶
   ``X-ANILA-Route``，那些回合的檢索命中絕不能漏進使用者看到的 payload——
   ``retrieve_institutional`` 連呼叫都不可以發生。
3. **門檻每個請求都重讀。** 設定頁改完下一個請求就要生效，否則那個設定就是
   假控制項的第一塊磚。
4. **沒有依據時不准裝作有。** ``searched_miss`` / ``search_error`` 注入的指示
   必含「不得以條號格式引用」——使用者信的是正文，不是標記。

⚠ **本檔用 stub 取代 ``retrieve_institutional``**（真模組的行為由
``tests/test_institutional_kb.py`` 驗，含密等過濾與五狀態的分界）。但 stub 一律
回**真的** ``KbResult`` / ``KbHit`` / ``KbState``，而且五個狀態**全部**都要跑到
線上（``_EVERY_STATE``，從 enum 推導不手寫清單）——只測 happy path 的話，
呼叫端自己重推狀態（把 SEARCH_ERROR 併進 PARTIAL_ERROR）不會有任何一支測試變紅。
"""

from __future__ import annotations

import json
import json as json_module  # ``post()`` 的參數名就叫 json，會遮蔽模組名
import os

# 同 house pattern（test_proxy_task_wiring.py）：endpoint 測試會啟動 app，
# startup_security 在 production 模式擋 dev 預設 secret — 測試環境放行。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import proxy as proxy_api
from app.models.agent import UserAgentPermission
from app.models.platform_setting import (
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_KEY,
    PlatformSetting,
)
from app.services import proxy_service
from app.services.auth_service import create_tokens
from app.services.institutional_kb import KbHit, KbResult, KbState
from app.services.proxy_service import build_default_anila_meta

from tests.conftest import make_agent, make_model, make_user

# ⚠ 從 enum 推導，不是自己列一份清單。將來多一個狀態，這裡要自動長出一輪
# 測試；手寫的清單只會停在寫的那一天，而且不會有任何東西提醒你。
_EVERY_STATE = list(KbState)

# 四個出口。少接一個，就會有一條分支靜默無狀態——而那條分支上的答案在 UI 上
# 看起來跟「查過院規、沒命中」一模一樣。
_EXITS = ["model_json", "model_sse", "agent_json", "agent_sse"]


# ── Fakes：記下 header **與 body**（注入的段落只在 body 裡看得到）─────────────


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _StreamResponse:
    def __init__(self, lines, status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """同時支援非串流 ``.post()`` 與串流 ``.stream()`` 的假上游。

    ``last_body`` 是本檔的主要證據來源：注入進系統訊息的規章段落只有在送給
    上游的 body 裡看得到，payload 上是看不到的。
    """

    last_body: dict | None = None
    last_headers: dict = {}
    stream_lines: list[str] | None = None
    # 下游自己回的 ``anila_meta``。設了之後 CSP 的骨架就**不會**被建出來
    # （proxy.py:1327-1328 的 ``if not existing_meta:``、service.py:635 同形），
    # 於是 kb_state 只能靠本層蓋上去——那是硬規則 1 唯一分得出
    # 「真的蓋了」與「骨架預設值剛好也是 not_searched」的路徑。
    post_meta: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_body = json
        type(self).last_headers = dict(headers or {})
        payload = {
            "choices": [
                {"message": {"role": "assistant", "content": "上游的回答"}}
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        }
        if type(self).post_meta is not None:
            payload["anila_meta"] = json_module.loads(
                json_module.dumps(type(self).post_meta)
            )
        return _PostResponse(payload)

    def stream(self, method, url, json=None, headers=None):
        type(self).last_body = json
        type(self).last_headers = dict(headers or {})
        lines = type(self).stream_lines
        if lines is None:
            lines = [
                'data: {"choices":[{"index":0,"delta":{"content":"上游的回答"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
                '"completion_tokens":2,"total_tokens":4}}',
                "",
                "data: [DONE]",
                "",
            ]
        return _StreamResponse(lines)


@pytest.fixture(autouse=True)
def _fake_upstream(monkeypatch):
    _FakeClient.last_body = None
    _FakeClient.last_headers = {}
    _FakeClient.stream_lines = None
    _FakeClient.post_meta = None
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FakeClient(*a, **k)
    )
    return _FakeClient


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    """Mock 上游是單標籤 http 主機名，照 test_proxy_stream_usage.py 的既有做法放行。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,agent")


class _KbStub:
    """``retrieve_institutional`` 的替身：記錄呼叫，回真的 ``KbResult``。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.result = KbResult(state=KbState.SEARCHED_MISS)
        self.raises: Exception | None = None

    async def __call__(self, db, user, query, *, threshold, top_k=8):
        self.calls.append(
            {"user": user, "query": query, "threshold": threshold, "top_k": top_k}
        )
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def kb(monkeypatch) -> _KbStub:
    stub = _KbStub()
    monkeypatch.setattr(proxy_api, "retrieve_institutional", stub)
    return stub


# ── 使用者 / 目標 ────────────────────────────────────────────────────────────


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(user) -> str:
    return create_tokens(user)["access_token"]


@pytest.fixture
def actor(db: Session):
    return make_user(db, username="kb_caller", role="admin")


@pytest.fixture
def model_target(db: Session):
    return make_model(db, name="kb-llm")


@pytest.fixture
def agent_target(db: Session, actor):
    dev = make_user(db, username="kb_dev", role="developer")
    agent = make_agent(db, dev, name="kb-agent", approval_status="approved")
    db.add(UserAgentPermission(user_id=actor.id, agent_id=agent.id))
    db.commit()
    return agent


def _chat(
    client: TestClient,
    actor,
    *,
    target: str,
    stream: bool = False,
    route: str | None = None,
    text: str = "出差搭高鐵可以報商務車廂嗎",
    extra_messages: list[dict] | None = None,
):
    headers = _bearer(_jwt(actor))
    if route is not None:
        headers["X-ANILA-Route"] = route
    messages = list(extra_messages or [])
    messages.append({"role": "user", "content": text})
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": target, "stream": stream, "messages": messages},
    )
    if stream:
        _ = resp.text  # 排空 SSE，讓串流真的跑完
    return resp


def _meta_frames(sse_text: str) -> list[dict]:
    frames: list[dict] = []
    for block in sse_text.split("\n\n"):
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event == "anila.meta" and data and data != "[DONE]":
            frames.append(json.loads(data))
    return frames


def _meta_of(resp, stream: bool) -> dict:
    """四個出口共用的取 meta 方式——出口不同，meta 的位置不同，狀態的要求相同。"""
    if stream:
        frames = _meta_frames(resp.text)
        assert frames, "串流出口一個 anila.meta frame 都沒有"
        return frames[-1]
    return resp.json()["anila_meta"]


def _run_exit(client, actor, model_target, agent_target, exit_kind, **kwargs):
    stream = exit_kind.endswith("sse")
    target = (
        model_target.name if exit_kind.startswith("model") else agent_target.name
    )
    resp = _chat(client, actor, target=target, stream=stream, **kwargs)
    assert resp.status_code == 200, resp.text
    return _meta_of(resp, stream)


def _system_text(body: dict) -> str:
    msgs = body["messages"]
    assert msgs[0]["role"] == "system", f"注入沒有落在系統訊息上：{msgs[0]}"
    content = msgs[0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content)


def _hit(
    n: int,
    *,
    collection_id: int = 1,
    score: float | None = None,
    image_pks: list[int] | None = None,
) -> KbHit:
    return KbHit(
        collection_id=collection_id,
        document_id=100 + n,
        filename=f"規章-{n}.pdf",
        content=f"第 {n} 段規章內容：出差搭乘高鐵以標準車廂為原則。",
        score=0.9 - n * 0.1 if score is None else score,
        image_pks=list(image_pks or []),
    )


# ── 1. 狀態明帶在每一個出口 ─────────────────────────────────────────────────


def test_state_is_always_present_in_the_payload(
    client, db, actor, model_target, kb
):
    """⚠ 第四狀態絕不能靠「payload 裡沒資料」表示。

    若缺席即 not_searched，渲染管線一壞，**所有答案都會靜默降級成沒查過** ——
    這是本專案的頭號家賊。
    """
    resp = _chat(client, actor, target=model_target.name)
    assert resp.status_code == 200, resp.text
    meta = resp.json()["anila_meta"]
    assert "kb_state" in meta, "kb_state 缺席——缺席不是 not_searched"
    assert meta["kb_state"] == "not_searched"


@pytest.mark.parametrize("exit_kind", _EXITS)
@pytest.mark.parametrize("state", _EVERY_STATE, ids=lambda s: s.value)
def test_all_four_payload_exits_carry_the_state(
    client, db, actor, model_target, agent_target, kb, exit_kind, state
):
    """payload 有三個建構點、四個出口，五個狀態都要原字串騎到每一個出口上。

    只接兩條會讓 agent 分支與一條串流分支靜默無狀態；只測一個狀態的話，
    呼叫端自行重推狀態（例如把 SEARCH_ERROR 併進 PARTIAL_ERROR）不會變紅。
    """
    hits = [_hit(1), _hit(2)] if state in (
        KbState.SEARCHED_HIT, KbState.PARTIAL_ERROR
    ) else []
    failed = [7] if state in (KbState.PARTIAL_ERROR, KbState.SEARCH_ERROR) else []
    kb.result = KbResult(state=state, hits=hits, failed_collections=failed)

    meta = _run_exit(
        client, actor, model_target, agent_target, exit_kind, route="direct"
    )
    assert meta["kb_state"] == state.value


@pytest.mark.parametrize("exit_kind", _EXITS)
def test_every_exit_says_not_searched_when_unmarked(
    client, db, actor, model_target, agent_target, kb, exit_kind
):
    """派工／ANILALM 的回合：四個出口都要明帶 not_searched（不是欄位不見）。"""
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])
    meta = _run_exit(client, actor, model_target, agent_target, exit_kind)
    assert meta["kb_state"] == "not_searched"
    assert not kb.calls


def test_build_default_anila_meta_declares_the_state(db):
    """meta 骨架自己就要明帶狀態——任何沒被 proxy 蓋過的 meta 也不會沒有欄位。"""
    meta = build_default_anila_meta("x", detail="d")
    assert meta["kb_state"] == KbState.NOT_SEARCHED.value


# ── 2. 無 header ＝ 一次檢索都不許發生 ──────────────────────────────────────


@pytest.mark.parametrize("exit_kind", _EXITS)
def test_no_header_means_no_retrieval_at_all(
    client, db, actor, model_target, agent_target, kb, exit_kind
):
    """Q39 的洩漏防線：無 header（派工／ANILALM）不只標 not_searched，
    ``retrieve_institutional`` 根本不得被呼叫——派工回合的命中絕不能漏進 payload。"""
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])
    _run_exit(client, actor, model_target, agent_target, exit_kind)
    assert kb.calls == [], "沒有 header 卻跑了檢索"
    body = _FakeClient.last_body
    assert "規章" not in json.dumps(body, ensure_ascii=False), (
        "沒有 header 的回合，規章內容漏進了送往上游的 body"
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_route_header_is_treated_as_absent(
    client, db, actor, model_target, kb, blank
):
    """空值不是「有標記」。router 永遠送正規化過的值，空值只可能來自別處。"""
    _chat(client, actor, target=model_target.name, route=blank)
    assert kb.calls == []


@pytest.mark.parametrize("route", ["direct", "forced"])
def test_both_marker_values_trigger_retrieval(
    client, db, actor, model_target, kb, route
):
    """``direct`` 與 ``forced`` 都是答案通道（task-5-report §1）。"""
    _chat(client, actor, target=model_target.name, route=route)
    assert len(kb.calls) == 1


# ── 3. 檢索的入參：查詢字串與門檻 ───────────────────────────────────────────


def test_the_query_is_the_latest_user_message(
    client, db, actor, model_target, kb
):
    """檢索的 query ＝ 使用者最新一則 user 訊息，與 captured_user_text 同定義。"""
    _chat(
        client,
        actor,
        target=model_target.name,
        route="direct",
        text="最新的問題",
        extra_messages=[
            {"role": "system", "content": "你是助理"},
            {"role": "user", "content": "很久以前的問題"},
            {"role": "assistant", "content": "很久以前的回答"},
        ],
    )
    assert kb.calls[0]["query"] == "最新的問題"


def test_the_retrieval_is_attributed_to_the_asking_user(
    client, db, actor, model_target, kb
):
    """嵌入用量歸戶到發問的人（institutional_kb 的 ``user`` 是必填不是方便參數）。"""
    _chat(client, actor, target=model_target.name, route="direct")
    assert kb.calls[0]["user"].id == actor.id


def test_threshold_change_takes_effect_next_request(
    client, db, actor, model_target, kb
):
    """設計 §7 的驗收釘：從設定改完，檢索行為真的變了（不重啟）——
    否則門檻設定就是假控制項的第一塊磚。"""
    _chat(client, actor, target=model_target.name, route="direct")
    assert kb.calls[-1]["threshold"] == KB_THRESHOLD_DEFAULT

    db.add(PlatformSetting(key=KB_THRESHOLD_KEY, value="0.71"))
    db.commit()
    _chat(client, actor, target=model_target.name, route="direct")
    assert kb.calls[-1]["threshold"] == pytest.approx(0.71)

    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    row.value = "0.42"
    db.commit()
    _chat(client, actor, target=model_target.name, route="direct")
    assert kb.calls[-1]["threshold"] == pytest.approx(0.42)


# ── 4. 注入的內容 ───────────────────────────────────────────────────────────


def test_miss_prompt_forbids_article_style_citation(
    client, db, actor, model_target, kb
):
    """使用者信的是正文不是標記。"""
    kb.result = KbResult(state=KbState.SEARCHED_MISS)
    _chat(client, actor, target=model_target.name, route="direct")
    system = _system_text(_FakeClient.last_body)
    assert proxy_api._KB_NO_CITATION_RULE in system
    assert proxy_api._KB_MISS_NOTICE in system
    # 硬規則 4 的另一半：**明令**以一般知識的口吻作答。只擋條號、不交代這是
    # 一般知識，模型仍然會用規章的語氣講一段沒有依據的話。
    assert "一般知識" in system


def test_search_error_prompt_is_not_the_miss_prompt(
    client, db, actor, model_target, kb
):
    """「查不了」與「查過沒有」是兩件事，注入的話也不可以是同一句。"""
    kb.result = KbResult(state=KbState.SEARCH_ERROR, failed_collections=[1, 2])
    _chat(client, actor, target=model_target.name, route="direct")
    system = _system_text(_FakeClient.last_body)
    assert proxy_api._KB_NO_CITATION_RULE in system
    assert "一般知識" in system
    assert proxy_api._KB_ERROR_NOTICE in system
    assert proxy_api._KB_MISS_NOTICE not in system


def test_hit_injects_numbered_passages_matching_citations(
    client, db, actor, model_target, kb
):
    """``[1]..[N]`` 與 ``citations[N-1]`` 對齊；每筆 citation 有非空 id 與 title
    （drawer 契約：空 id 會弄壞 CitationsDrawer，不只是樣式）。"""
    kb.result = KbResult(
        state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)]
    )
    resp = _chat(client, actor, target=model_target.name, route="direct")
    system = _system_text(_FakeClient.last_body)
    meta = resp.json()["anila_meta"]

    citations = meta["citations"]
    assert len(citations) == 2
    for idx, cit in enumerate(citations, start=1):
        marker = f"[{idx}]"
        assert marker in system, f"注入段落缺少 {marker}"
        assert cit["id"], "citation 少了 id —— drawer 靠它對位"
        assert cit["title"]
    # 順序對齊：第 1 段的內容要出現在 [1] 之後、[2] 之前。
    first = system.index("[1]")
    second = system.index("[2]")
    assert first < system.index(_hit(1).content) < second
    assert second < system.index(_hit(2).content)
    assert citations[0]["title"] == _hit(1).filename
    assert citations[1]["title"] == _hit(2).filename
    assert citations[0]["id"] != citations[1]["id"]


def test_hit_with_image_pks_lands_on_citations_without_urls(
    client, db, actor, model_target, kb
):
    """Figure ids ride the existing citation object — not a parallel channel,
    and not a URL in the embed/snippet text."""
    kb.result = KbResult(
        state=KbState.SEARCHED_HIT,
        hits=[_hit(1, image_pks=[42]), _hit(2)],
    )
    resp = _chat(client, actor, target=model_target.name, route="direct")
    citations = resp.json()["anila_meta"]["citations"]
    assert citations[0]["image_pks"] == [42]
    assert "image_pks" not in citations[1]
    snippet = citations[0]["snippet"]
    assert "http://" not in snippet
    assert "https://" not in snippet
    assert "/api/ingestion" not in snippet
    assert "![" not in snippet


def test_two_chunks_of_the_same_document_get_distinct_citation_ids(
    client, db, actor, model_target, kb
):
    """命中是 chunk 級的，同一份文件可以中兩段——id 撞在一起 drawer 會對錯訊息
    （app.jsx 用 id 跨訊息找來源）。"""
    same = _hit(1)
    twin = KbHit(
        collection_id=same.collection_id,
        document_id=same.document_id,
        filename=same.filename,
        content="同一份文件的另一段內容。",
        score=0.5,
    )
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[same, twin])
    resp = _chat(client, actor, target=model_target.name, route="direct")
    ids = [c["id"] for c in resp.json()["anila_meta"]["citations"]]
    assert len(set(ids)) == 2, f"citation id 撞號：{ids}"


def test_partial_error_reports_the_failed_collections(
    client, db, actor, model_target, kb
):
    """有一庫沒查成就不可以裝成「這就是全部的依據」。"""
    kb.result = KbResult(
        state=KbState.PARTIAL_ERROR,
        hits=[_hit(1), _hit(2)],
        failed_collections=[3, 9],
    )
    resp = _chat(client, actor, target=model_target.name, route="direct")
    meta = resp.json()["anila_meta"]
    assert meta["kb_state"] == "partial_error"
    assert meta["kb_failed_collections"] == [3, 9]
    assert len(meta["citations"]) == 2


def test_not_searched_injects_nothing(
    client, db, actor, model_target, kb
):
    """一個庫都沒標記時不要對每一通聊天加話——沒開這個功能的院所不該被改變行為。"""
    kb.result = KbResult(state=KbState.NOT_SEARCHED)
    _chat(client, actor, target=model_target.name, route="direct")
    body = _FakeClient.last_body
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_hits_ride_the_system_message_only(
    client, db, actor, model_target, kb
):
    """來源可分辨性（Task 5 複審的設計後果）：被標記的多輪合成呼叫裡，對話中
    已經有 agent 的輸出。規章內容只准騎系統訊息，**不得**與 assistant／user 回合
    交錯——這樣規章段落與 agent 文字靠結構就分得開，不必靠字面猜。
    """
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])
    prior = [
        {"role": "user", "content": "上一輪的問題"},
        {"role": "assistant", "content": "Agent 'x' responded: 這是 agent 寫的字"},
    ]
    _chat(
        client,
        actor,
        target=model_target.name,
        route="direct",
        text="這一輪的問題",
        extra_messages=prior,
    )
    msgs = _FakeClient.last_body["messages"]
    assert msgs[0]["role"] == "system"
    # 系統訊息以外，每一則都必須與送進來的一模一樣。
    assert msgs[1:] == [*prior, {"role": "user", "content": "這一輪的問題"}]
    for msg in msgs[1:]:
        assert _hit(1).content not in str(msg["content"])
        assert _hit(2).content not in str(msg["content"])


def test_existing_system_prompt_is_kept(
    client, db, actor, model_target, kb
):
    """注入是 append（照 ``_inject_memory`` 樣板），不是覆蓋，也不再 prepend：
    呼叫端的靜態前導必須留在最前面（harness §6-1，2026-09-02）。"""
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])
    _chat(
        client,
        actor,
        target=model_target.name,
        route="direct",
        extra_messages=[{"role": "system", "content": "原本的系統提示"}],
    )
    system = _system_text(_FakeClient.last_body)
    assert "原本的系統提示" in system
    assert system.startswith("原本的系統提示")
    assert system.index("原本的系統提示") < system.index(_hit(1).content)
    assert system.rstrip().endswith(proxy_api.KB_LANGUAGE_REMINDER)


# ── 5. 失敗不擋回答 ─────────────────────────────────────────────────────────


def test_search_error_does_not_block_the_answer(
    client, db, actor, model_target, kb
):
    """設計 §5：檢索失敗明示，但照答。"""
    kb.result = KbResult(state=KbState.SEARCH_ERROR, failed_collections=[1])
    resp = _chat(client, actor, target=model_target.name, route="direct")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["choices"][0]["message"]["content"] == "上游的回答"
    assert payload["anila_meta"]["kb_state"] == "search_error"


def test_an_exploding_retrieval_still_answers(
    client, db, actor, model_target, kb
):
    """模組整個炸掉（不是回 SEARCH_ERROR 而是拋例外）也不可以讓聊天掛掉。"""
    kb.raises = RuntimeError("boom")
    resp = _chat(client, actor, target=model_target.name, route="direct")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["choices"][0]["message"]["content"] == "上游的回答"
    assert payload["anila_meta"]["kb_state"] == "search_error"


# ── 6. SSE 出口的機制（直接對 wrapper 下手）─────────────────────────────────


async def _drain(agen) -> str:
    return "".join([chunk async for chunk in agen])


async def _agen(chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_sse_wrapper_stamps_a_forwarded_meta_frame():
    """下游自己送 anila.meta 時，proxy_stream 不會再合成一個——狀態要蓋在那一個上。"""
    frame = (
        "event: anila.meta\n"
        + "data: "
        + json.dumps({"citations": [{"id": "x", "title": "下游的來源"}]})
        + "\n\n"
    )
    out = await _drain(
        proxy_api._sse_with_kb_meta(
            _agen([frame, "data: [DONE]\n\n"]),
            {"kb_state": "searched_hit", "citations": [{"id": "kb:1", "title": "規章"}]},
        )
    )
    metas = _meta_frames(out)
    assert len(metas) == 1
    assert metas[0]["kb_state"] == "searched_hit"
    # 下游的 citation 不可以被吃掉；我們的排在前面，[N] 才對得上。
    assert [c["id"] for c in metas[0]["citations"]] == ["kb:1", "x"]


@pytest.mark.asyncio
async def test_sse_wrapper_synthesises_a_meta_frame_when_there_is_none():
    """一個 anila.meta 都沒有的串流，狀態仍然要送到——而且要在 [DONE] 之前。"""
    out = await _drain(
        proxy_api._sse_with_kb_meta(
            _agen(['data: {"choices":[]}\n\n', "data: [DONE]\n\n"]),
            {"kb_state": "searched_miss"},
        )
    )
    metas = _meta_frames(out)
    assert len(metas) == 1
    assert metas[0]["kb_state"] == "searched_miss"
    assert out.index("anila.meta") < out.index("[DONE]")


@pytest.mark.asyncio
async def test_sse_wrapper_passes_everything_else_through():
    chunks = ['data: {"a":1}\n\n', 'data: {"b":2}\n\n', "data: [DONE]\n\n"]
    out = await _drain(
        proxy_api._sse_with_kb_meta(_agen(chunks), {"kb_state": "not_searched"})
    )
    for chunk in chunks:
        assert chunk.strip() in out


# ── 7. 下游自帶 anila_meta：狀態仍然必須是本層蓋上去的 ───────────────────────


def _downstream_meta_lines(meta: dict) -> list[str]:
    """下游自己送一個 anila.meta frame 的串流（``meta_seen`` → 骨架不會被合成）。"""
    return [
        'data: {"choices":[{"index":0,"delta":{"content":"上游的回答"},'
        '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
        '"completion_tokens":2,"total_tokens":4}}',
        "",
        "event: anila.meta",
        "data: " + json.dumps(meta, ensure_ascii=False),
        "",
        "data: [DONE]",
        "",
    ]


@pytest.mark.parametrize("exit_kind", _EXITS)
@pytest.mark.parametrize(
    "route, expected",
    [(None, "not_searched"), ("direct", "searched_hit")],
    ids=["unmarked", "marked"],
)
def test_downstream_supplied_meta_still_carries_the_state(
    client, db, actor, model_target, agent_target, kb, exit_kind, route, expected
):
    """⚠ 硬規則 1 的**核心釘子**：狀態必須是本層真的蓋上去的，不可以只是
    「骨架的預設值剛好也是 not_searched」。

    下游（agent 或模型）自己回了 ``anila_meta`` 時，CSP 的骨架**根本不會被建
    出來**（proxy.py:1327-1328、service.py:635 都是 ``if not existing_meta:``），
    串流那邊 ``meta_seen`` 也會讓 proxy_stream 不再合成。這條路徑上「靠缺席
    表示 not_searched」會讓欄位**整個消失**，而畫面上與「查過、沒命中」
    一模一樣——那正是本檔開頭第 1 條寫的頭號家賊。
    """
    downstream = {
        "citations": [{"id": "ds-1", "title": "下游自己的來源"}],
        "trace": [{"kind": "call", "label": "下游", "detail": "d", "status": "ok"}],
    }
    _FakeClient.post_meta = downstream
    _FakeClient.stream_lines = _downstream_meta_lines(downstream)
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])

    meta = _run_exit(
        client, actor, model_target, agent_target, exit_kind, route=route
    )
    assert "kb_state" in meta, "下游自帶 meta 時 kb_state 整個不見了"
    assert meta["kb_state"] == expected
    # 下游自己的來源不可以被吃掉（我們的排前面，[N] 才對得上）。
    assert any(c["id"] == "ds-1" for c in meta["citations"])
    if expected == "searched_hit":
        assert [c["id"] for c in meta["citations"]][-1] == "ds-1"
        assert len(meta["citations"]) == 3


# ── 8. trace 是第三個表面，也要說同一個故事 ─────────────────────────────────


def _kb_trace(meta: dict) -> dict | None:
    for entry in meta.get("trace") or []:
        if isinstance(entry, dict) and entry.get("kind") == "institutional_kb":
            return entry
    return None


@pytest.mark.parametrize(
    "state, failed, expected_status",
    [
        (KbState.SEARCHED_HIT, [], "ok"),
        (KbState.SEARCHED_MISS, [], "ok"),
        (KbState.PARTIAL_ERROR, [3], "partial"),
        (KbState.SEARCH_ERROR, [3], "error"),
    ],
    ids=lambda v: str(v),
)
def test_the_trace_entry_tells_the_same_story_as_the_state(
    client, db, actor, model_target, kb, state, failed, expected_status
):
    """trace 是使用者真的會讀到的**第三個表面**（``apps/anila-shell/src/chat.jsx``
    的 RoutingTrace／ReasoningSummary 逐則渲染）。payload 與提示詞都套了誠實
    紀律，trace 漏掉就會出現「payload 說查不了、畫面上寫查過沒有」——設計 §5
    「沒命中與查不了是兩件事」的逐字違反。
    """
    hits = [_hit(1), _hit(2)] if state in (
        KbState.SEARCHED_HIT, KbState.PARTIAL_ERROR
    ) else []
    kb.result = KbResult(state=state, hits=hits, failed_collections=failed)
    resp = _chat(client, actor, target=model_target.name, route="direct")
    entry = _kb_trace(resp.json()["anila_meta"])
    assert entry is not None, f"{state.value} 沒有留下 trace 條目"
    assert entry["status"] == expected_status
    assert entry["label"] == proxy_api._KB_TRACE_LABEL


def test_the_trace_never_calls_a_failure_a_miss(
    client, db, actor, model_target, kb
):
    """「查不了」不可以在 trace 上被寫成「查過、沒有」——兩個狀態就這樣從
    使用者那邊消失了。"""
    kb.result = KbResult(state=KbState.SEARCH_ERROR, failed_collections=[1])
    resp = _chat(client, actor, target=model_target.name, route="direct")
    entry = _kb_trace(resp.json()["anila_meta"])
    assert entry["status"] == "error"
    assert "失敗" in entry["detail"]
    assert "沒有相關條文" not in entry["detail"]

    kb.result = KbResult(state=KbState.SEARCHED_MISS)
    resp = _chat(client, actor, target=model_target.name, route="direct")
    miss_entry = _kb_trace(resp.json()["anila_meta"])
    assert miss_entry["status"] == "ok"
    assert miss_entry["detail"] != entry["detail"]


def test_not_searched_leaves_no_trace_entry(
    client, db, actor, model_target, kb
):
    """沒搜過就不要在每一通聊天的 trace 上多一行（也不可以留一行說搜過了）。"""
    resp = _chat(client, actor, target=model_target.name)
    assert _kb_trace(resp.json()["anila_meta"]) is None


# ── 9. partial 的「這不是全部的依據」必須進到提示詞 ──────────────────────────


def test_partial_error_tells_the_model_the_basis_is_incomplete(
    client, db, actor, model_target, kb
):
    """payload 誠實還不夠：模型手上少了一庫卻沒被告知，就會照著這幾段當成
    完整依據作答，而使用者讀到的是那段話，不是 meta 欄位。"""
    kb.result = KbResult(
        state=KbState.PARTIAL_ERROR,
        hits=[_hit(1), _hit(2)],
        failed_collections=[3, 9],
    )
    _chat(client, actor, target=model_target.name, route="direct")
    system = _system_text(_FakeClient.last_body)
    assert proxy_api._KB_PARTIAL_NOTICE.format(n=2) in system


def test_a_clean_hit_does_not_claim_the_basis_is_incomplete(
    client, db, actor, model_target, kb
):
    """反向：全庫都查成時不可以無中生有地說檢索不完整。"""
    kb.result = KbResult(state=KbState.SEARCHED_HIT, hits=[_hit(1), _hit(2)])
    _chat(client, actor, target=model_target.name, route="direct")
    system = _system_text(_FakeClient.last_body)
    # ⚠ 斷言的字串必須真的是 ``_KB_PARTIAL_NOTICE`` 裡有的片段，否則這條是
    # 永遠不會紅的假斷言（markdown 的星號位置很容易抄錯一格）。
    assert "全部的依據" in proxy_api._KB_PARTIAL_NOTICE
    assert "查詢失敗" in proxy_api._KB_PARTIAL_NOTICE
    assert "全部的依據" not in system
    assert "查詢失敗" not in system
