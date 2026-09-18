# -*- coding: utf-8 -*-
"""院內規章檢索的分數門檻：一個**設定**，外加一個看得到證據的校準視圖。

這一支釘的不是「門檻有沒有生效」，而是**設定頁那個大工程的第一塊磚會不會是
假的**。畫面上改了一個數字、後端還在讀舊值，是本平台可能出的最大的假控制項
（`docs/FAKE-CONTROLS.md` 整份講的就是這件事）。所以本檔第一支測試不是驗
「存得進去」，是驗「同一個行程內改完就生效」——那正是任何形式的行程生命期
快取會當場死掉的地方。門檻之後還有 41 個環境變數要照這個形狀搬過來，這裡
證不出來的東西，後面 41 次都證不出來。

其餘四條不變式：

* **給數字輸入框就要給證據。** 校準視圖要回實際分數與命中內容，否則叫人猜。
* **校準走的是正式檢索那條路。** 自己寫一份「比較寬鬆」的檢索 = 校準看到的
  不是使用者會看到的；密等過濾也會在那一份裡消失。
* **預設值 0.3 必須自認未校準。** 它是拿替代模型量的（`PLAN.md:77`），對真正
  的 nv-embed 沒有意義。假裝它是已知數，就沒有人會去量。
* **門檻是密等相鄰設定** —— 改它等於改全院檢索的鬆緊，admin 限定。

⚠ **本檔刻意不需要 PostgreSQL**：向量層照 ``tests/test_institutional_kb.py``
既有做法以 stub 取代（SQLite 跑不動 asyncpg + halfvec）。stub 與該檔重複而不是
import 過來，是因為跨測試檔共用 stub 會讓一邊的調整靜默改變另一邊的驗收範圍。

⚠ fixture 一律 function-scoped：本檔會改同一列 collection 與 platform_settings
的內容，共用會讓前一支的殘留變成下一支的前置條件。
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

import app.services.institutional_kb as kb_mod
from anila_core.storage.adapters.pgvector_store import SourceModelCoverage
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.platform_setting import (
    KB_THRESHOLD_CALIBRATED_AT_KEY,
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_EMBEDDING_KEY,
    KB_THRESHOLD_KEY,
    PlatformSetting,
    set_kb_threshold,
    set_setting,
)
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from tests.conftest import login, make_user

_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.to_storage()
# ⚠ 從 enum 推導，不是自己列一份清單：只造一級密等的 fixture 會讓其餘每一級
# 都沒有人看著，而測試全綠。本專案已經因為同一個形狀被抓兩次。
_CLASSIFIED_LEVELS = [
    level.to_storage()
    for level in ClassificationLevel
    if level is not ClassificationLevel.UNCLASSIFIED
]
_EMBED_MODEL = "nvidia/nv-embed-v2"
_DIM = 8
# fixture 埋的那一筆命中的分數。落在預設門檻 0.3 之上、0.99 之下，所以
# 「門檻 0.0 → 有、門檻 0.99 → 沒有」是同一筆資料的兩個答案，不是兩份資料。
_HIT_SCORE = 0.42

_THRESHOLD_URL = "/api/institutional-kb/threshold"
_PREVIEW_URL = "/api/institutional-kb/preview"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── DB fixtures ─────────────────────────────────────────────────────────────


def _owner(db) -> User:
    user = User(
        username=f"kb-threshold-owner-{uuid.uuid4().hex[:12]}",
        hashed_password="x",
        role="developer",
        is_approved=True,
    )
    db.add(user)
    db.flush()
    return user


def _collection(db, name: str, *, searchable: bool = True) -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model=_EMBED_MODEL,
        embedding_dim=_DIM,
        status="active",
        created_by=_owner(db).id,
        origin="csp",
        classification_level=_UNCLASSIFIED,
        anila_searchable=searchable,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _document(db, coll: IngestionCollection, filename: str, level: str) -> IngestionDocument:
    doc = IngestionDocument(
        collection_id=coll.id,
        filename=filename,
        sha256=f"{abs(hash(filename)):064x}"[:64],
        mime_type="application/pdf",
        status="indexed",
        classification_level=level,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture()
def admin_token(client, db) -> str:
    make_user(db, username="kb_threshold_admin", role="admin")
    return login(client, "kb_threshold_admin")


@pytest.fixture()
def plain_token(client, db) -> str:
    make_user(db, username="kb_threshold_plain", role="developer")
    return login(client, "kb_threshold_plain")


# ── 向量層 stub ─────────────────────────────────────────────────────────────


class _StubChunk:
    def __init__(self, document_id: int, content: str):
        self.id = document_id * 100
        self.document_id = document_id
        self.chunk_key = f"chunk:{document_id}"
        self.content = content
        self.metadata: dict = {}
        self.parent_chunk_id = None
        self.chunk_type = "leaf"
        self.chunk_level = 0


class _StubHit:
    def __init__(self, document_id: int, score: float, content: str = "第三條 申誡三次視同記過一次。"):
        self.chunk = _StubChunk(document_id, content)
        self.score = score
        self.parent_content = None


class _Backend:
    """假的向量後端，逐庫回答。

    ``similarity_search`` **必須真的套用 min_score** —— 門檻沒被一路傳下去的話，
    本檔第一支測試（改完即生效）就是假的：兩次 preview 會回一樣的東西，而
    測試只會看到「都有命中」。
    """

    def __init__(self):
        self.hits: dict[int, list[_StubHit]] = {}
        self.coverage: dict[int, SourceModelCoverage] = {}
        self.constructed: list[int] = []
        self.min_scores: list[float] = []

    def _store(self, pool, collection_id: int):
        self.constructed.append(collection_id)
        return _StubStore(self, collection_id)

    def install(self, monkeypatch) -> "_Backend":
        monkeypatch.setattr(kb_mod, "get_pool", lambda: object())
        monkeypatch.setattr(
            kb_mod,
            "CollectionScopedPgVectorStore",
            lambda pool, collection_id: self._store(pool, collection_id),
        )
        return self


class _StubStore:
    def __init__(self, backend: _Backend, collection_id: int):
        self._backend = backend
        self._collection_id = collection_id

    async def similarity_search(self, *, query_embedding, top_k, min_score, source_model=None):
        self._backend.min_scores.append(min_score)
        hits = self._backend.hits.get(self._collection_id, [])
        return [h for h in hits if h.score >= min_score][:top_k]

    async def source_model_coverage(self, source_model: str) -> SourceModelCoverage:
        return self._backend.coverage.get(
            self._collection_id,
            SourceModelCoverage(has_matching=True, has_other=False, sample_other_model=None),
        )


@pytest.fixture()
def backend(monkeypatch) -> _Backend:
    return _Backend().install(monkeypatch)


@pytest.fixture(autouse=True)
def stub_embed(monkeypatch):
    """嵌入走 CSP proxy（會計量到人身上），測試裡不打網路。"""

    async def fake_embed_query(db, user, model_name, embedding_dim, query):
        return [0.1] * embedding_dim

    monkeypatch.setattr(kb_mod, "_embed_query", fake_embed_query)


@pytest.fixture()
def marked_collection(db, backend) -> IngestionCollection:
    """一個已標記、內容無機密的院規庫，後端埋一筆 0.42 分的命中。"""
    coll = _collection(db, "人事規章")
    doc = _document(db, coll, "獎懲作業要點.pdf", _UNCLASSIFIED)
    backend.hits[coll.id] = [_StubHit(doc.id, _HIT_SCORE)]
    coll._doc_ids = [doc.id]
    return coll


@pytest.fixture()
def marked_collection_with_classified_doc(db, backend, request) -> IngestionCollection:
    """已標記的庫裡混著一份帶密等的文件。

    ⚠ 這不是造假狀態：標記時的四道檢查是 T 時刻的檢查，文件是 T+1 才加進去的，
    而 DB CHECK 只鎖 collection 那一列。密等 由 ``request.param`` 逐級帶入。
    """
    level = request.param
    coll = _collection(db, "人事規章")
    clean = _document(db, coll, "獎懲作業要點.pdf", _UNCLASSIFIED)
    classified = _document(db, coll, f"個案調查報告-{level}.pdf", level)
    # 兩筆都埋：只埋帶密等那一筆的話，一個「什麼都不回」的壞實作也會綠。
    backend.hits[coll.id] = [
        _StubHit(classified.id, 0.99, content=f"{level}：個案當事人姓名"),
        _StubHit(clean.id, _HIT_SCORE),
    ]
    coll.classified_doc_id = classified.id
    coll.clean_doc_id = clean.id
    return coll


# ── 設定頁那件大工程的第一塊磚 ──────────────────────────────────────────────


def test_threshold_change_takes_effect_without_restart(
    client, admin_token, marked_collection
):
    """⚠ 這是「設定頁」那件大工程的第一塊磚。改了畫面卻不影響行為 = 假控制項。

    門檻之後還有 41 個環境變數要搬成設定。任何形式的行程生命期快取（模組層
    變數、``lru_cache``、啟動時讀一次）都會讓這一支變紅——那正是它存在的理由。
    """
    low = client.put(_THRESHOLD_URL, json={"value": 0.0}, headers=_auth(admin_token))
    assert low.status_code == 200, low.text
    before = client.post(
        _PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token)
    )
    assert before.status_code == 200, before.text
    assert before.json()["hits"], "門檻 0 應該有命中"
    assert before.json()["threshold"] == 0.0

    high = client.put(_THRESHOLD_URL, json={"value": 0.99}, headers=_auth(admin_token))
    assert high.status_code == 200, high.text
    after = client.post(
        _PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token)
    )
    assert after.status_code == 200, after.text
    assert after.json()["hits"] == [], "同一個行程內就要生效，不重啟"
    assert after.json()["threshold"] == 0.99
    # 同一筆資料的兩個答案，不是「第二次剛好沒東西」。
    assert after.json()["state"] == "searched_miss"


def test_preview_returns_scores_so_an_admin_can_calibrate(
    client, admin_token, marked_collection
):
    """給數字輸入框而不給證據，等於叫人猜。"""
    r = client.post(_PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    assert hits, "有命中才談得上校準"
    assert all("score" in h and "content" in h for h in hits)
    # 分數要是真的分數，不是佔位的 0 或 1 —— 校準要靠它挑門檻。
    assert hits[0]["score"] == pytest.approx(_HIT_SCORE)
    assert hits[0]["filename"] == "獎懲作業要點.pdf"
    assert hits[0]["collection_id"] == marked_collection.id


def test_threshold_is_admin_only(client, plain_token):
    """門檻是密等相鄰設定：改它等於改全院檢索的鬆緊。整個 router 都要關上。"""
    r = client.put(
        _THRESHOLD_URL, json={"value": 0.5}, headers=_auth(plain_token)
    )
    assert r.status_code == 403
    # 校準視圖會把院規內容整段回出來，讀也一樣要 admin。
    assert client.get(_THRESHOLD_URL, headers=_auth(plain_token)).status_code == 403
    assert (
        client.post(
            _PREVIEW_URL, json={"query": "申誡"}, headers=_auth(plain_token)
        ).status_code
        == 403
    )


def test_default_is_declared_uncalibrated(client, admin_token):
    """PLAN.md:77 —— 手上的 0.3 是用替代模型量的，對真 nv-embed 必須重校。
    不准假裝它是已知數。"""
    r = client.get(_THRESHOLD_URL, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == 0.3
    assert body["calibrated"] is False


def test_setting_the_threshold_marks_it_calibrated(client, admin_token):
    """上一支的另一半。

    少了這一支，把 ``calibrated`` 硬寫成 ``False`` 也會全綠——而那個實作等於
    「永遠說沒校準」，跟永遠說已校準一樣沒有資訊。有人真的看過分數、按下
    儲存之後，這個旗標必須翻面。
    """
    assert client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()[
        "calibrated"
    ] is False

    put = client.put(_THRESHOLD_URL, json={"value": 0.55}, headers=_auth(admin_token))
    assert put.status_code == 200, put.text

    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == 0.55
    assert body["calibrated"] is True


def test_out_of_range_is_refused_with_a_usable_message(client, admin_token):
    """相似度分數的定義域是 [0, 1]；界外值不是「比較嚴格」，是壞掉。"""
    for bad in (-0.1, 1.1):
        r = client.put(
            _THRESHOLD_URL, json={"value": bad}, headers=_auth(admin_token)
        )
        assert r.status_code in (400, 422), (bad, r.text)
        # 拒絕要給做法：訊息裡要有合法範圍，看的人才知道該填什麼。
        assert "0.0" in r.text and "1.0" in r.text, r.text

    # ⚠ 報了錯卻已經存進去，比靜默成功更難查。被拒絕的值不可以留下痕跡。
    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == 0.3
    assert body["calibrated"] is False


# ── 校準視圖與正式檢索必須是同一條路 ────────────────────────────────────────


@pytest.mark.parametrize(
    "marked_collection_with_classified_doc", _CLASSIFIED_LEVELS, indirect=True
)
def test_preview_never_leaks_a_classified_document(
    client, admin_token, marked_collection_with_classified_doc
):
    """校準視圖跟正式檢索走同一條路，不可以有自己的較寬鬆版本。

    ⚠ **逐級跑遍整個 enum**：只驗「機密」的話，把過濾抄成黑名單的實作照樣全綠，
    而營業秘密與密會外洩。密等的級數是 enum 的事，不是這支測試可以自己挑的。

    admin 看得到機密不代表校準視圖該回它：這個端點回的是**使用者提問時會拿到
    的東西**，它多回一筆就代表兩條路已經分岔。
    """
    coll = marked_collection_with_classified_doc
    r = client.post(_PREVIEW_URL, json={"query": "任何字"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]

    assert all(h["document_id"] != coll.classified_doc_id for h in hits)
    assert all("個案當事人姓名" not in h["content"] for h in hits)
    # 無機密那一筆要在 —— 否則一個「永遠回空」的實作也會通過上面兩條。
    assert [h["document_id"] for h in hits] == [coll.clean_doc_id]


# ── 顯示的數字必須就是生效的數字 ────────────────────────────────────────────


def _write_row_bypassing_the_api(db, raw: str) -> None:
    """繞過 API 直接寫一列 —— 匯入腳本、手動 SQL、backfill 都是這樣進來的。

    這條路是真的（``platform_settings`` 沒有 CHECK，那是刻意的取捨），所以
    「壞值進來之後平台怎麼表現」不是假想情境。
    """
    db.add(PlatformSetting(key=KB_THRESHOLD_KEY, value=raw))
    db.commit()


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, KB_THRESHOLD_DEFAULT),  # 沒有列
        ("0.55", 0.55),  # 正常值
        ("2.5", KB_THRESHOLD_DEFAULT),  # 界外：退回預設
        ("abc", KB_THRESHOLD_DEFAULT),  # 不是數字：退回預設
    ],
)
def test_the_number_on_screen_is_the_number_retrieval_uses(
    client, db, admin_token, marked_collection, backend, stored, expected
):
    """畫面上的門檻與檢索實際套用的門檻，必須是同一個數字。

    ⚠ 這一支補的是驗收找到的洞：原本「顯示 = 生效」只是因為兩個呼叫端**碰巧**
    都走 ``get_kb_threshold``，沒有任何東西會在它們分岔時叫出來。把讀取端改成
    自己 ``float(row.value)``（值壞掉時顯示 2.5、檢索卻用 0.3）——十支測試全綠。

    所以這裡不比「顯示的值等於某個預期常數」，而是比**顯示的值等於真正傳進
    ``similarity_search`` 的 ``min_score``**。那是唯一不會跟著實作一起漂的參照點。
    設定頁後面還有 41 個開關要照這個形狀證明。
    """
    if stored is not None:
        _write_row_bypassing_the_api(db, stored)

    shown = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()["value"]
    r = client.post(_PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    assert backend.min_scores, "檢索要真的跑過，否則下面比的是空氣"
    assert backend.min_scores[-1] == shown, (
        f"畫面顯示 {shown}，檢索實際用的是 {backend.min_scores[-1]}"
    )
    assert r.json()["threshold"] == shown
    assert shown == expected


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_what_a_successful_put_saved_is_what_retrieval_runs_on(
    client, db, admin_token, marked_collection, backend, value
):
    """收得下來的值，必須就是算得出來的值。

    ⚠ 這一支補的是第二輪驗收找到的洞，而它是「顯示 = 生效」那條的兄弟：
    把**解析端**的上界改成 exclusive（``0.0 <= v < 1.0``）而寫入端維持 inclusive，
    19 支測試全綠。實際行為是 ``PUT {"value": 1.0}`` 回 **200**、``'1.0'`` 真的存進
    DB，然後解析端判定它不可用 → 檢索跑預設值 0.3、畫面說「沒有人校準過」。
    管理員存的那個數字被靜默丟掉，只留一行 log。

    ⚠ 邊界值 **0.0 與 1.0 兩端都要跑**：原本只有 0.0 有人走過，而拒絕路徑只用
    ``-0.1`` / ``1.1`` 驗——正好繞開了「收與算可能不同意」的那兩個點。
    1.0（只認完全相同、實質上什麼都不收）是校準中的管理員合法會按的東西，
    跟 0.0（全收）是同一件事的兩端，所以值域兩端都收，兩端都要證明存得住。
    """
    put = client.put(_THRESHOLD_URL, json={"value": value}, headers=_auth(admin_token))
    assert put.status_code == 200, put.text
    # 回應就是重新解析出來的結果 —— 存進去卻算不出來的值在這裡就會現形。
    assert put.json()["value"] == value, put.text
    assert put.json()["calibrated"] is True, put.text

    # 真的落到那一列上（不是只是回應長得對）。
    db.expire_all()
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    assert row is not None and float(row.value) == value

    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == value
    assert body["calibrated"] is True

    r = client.post(_PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["threshold"] == value
    assert backend.min_scores, "檢索要真的跑過，否則下面比的是空氣"
    assert backend.min_scores[-1] == value, (
        f"存進去的是 {value}，檢索實際用的是 {backend.min_scores[-1]}"
    )


@pytest.mark.parametrize("stored", ["2.5", "-0.2", "abc", ""])
def test_a_stored_value_that_cannot_be_used_is_not_calibrated(
    client, db, admin_token, stored
):
    """壞值退回預設值時，``calibrated`` 必須跟著翻回 false。

    ⚠ 這是「有列就算校準」那個判準真正會騙到人的地方：實際跑的是那個沒有人
    量過的預設值，而畫面說「有人校準過」。管理員因此不會去量——這個旗標存在
    的唯一理由就是讓他去量。

    ⚠ 空字串也在清單裡：匯入把欄位清空是最常見的那一種，而 ``float("")``
    跟 ``float("abc")`` 走的是同一條例外路徑，漏掉一條就整條沒守。
    """
    _write_row_bypassing_the_api(db, stored)

    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == KB_THRESHOLD_DEFAULT
    assert body["calibrated"] is False, f"{stored!r} 退回預設值了，不可以說已校準"


def test_storing_exactly_the_default_still_counts_as_calibrated(client, admin_token):
    """按下儲存的是 0.3 也算校準過 —— 差別在於有沒有人看過證據，不在數字。

    ⚠ 這條保證原本只寫在報告裡、沒有測試守著：把判準改成「值不等於預設值才算
    校準」，十支測試全綠。那個實作會讓「我看過分數，確認 0.3 就是對的」這個
    結論**存不進系統**，下一個人打開設定頁看到的還是「沒有人量過」。
    """
    put = client.put(
        _THRESHOLD_URL, json={"value": KB_THRESHOLD_DEFAULT}, headers=_auth(admin_token)
    )
    assert put.status_code == 200, put.text

    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == KB_THRESHOLD_DEFAULT
    assert body["calibrated"] is True


def test_preview_says_not_searched_when_no_library_is_marked(client, admin_token, backend):
    """一個庫都沒標記時，校準視圖不可以顯示成「搜過了、沒東西」。

    看到 ``searched_miss`` 的管理員會去調門檻；真正的問題是他還沒標記任何庫，
    調到 0 也一樣是空的。狀態分錯一格，人就往錯的方向修一整個下午。
    """
    r = client.post(_PREVIEW_URL, json={"query": "申誡"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "not_searched"
    assert r.json()["hits"] == []
    assert backend.constructed == []


def test_calibrated_is_false_after_embedding_model_changes(client, admin_token, db):
    """門檻是跟著當時的嵌入模型量的。換模型之後不可以還說已校準。"""
    from app.models.model_registry import ModelRegistry

    first = ModelRegistry(
        name="emb-a",
        display_name="emb-a",
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=True,
        embedding_native_dim=8,
    )
    db.add(first)
    db.commit()

    put = client.put(_THRESHOLD_URL, json={"value": 0.4}, headers=_auth(admin_token))
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["calibrated"] is True
    assert body["calibrated_with_embedding_model"] == "emb-a"

    first.is_platform_embedding = False
    second = ModelRegistry(
        name="emb-b",
        display_name="emb-b",
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=True,
        embedding_native_dim=8,
    )
    db.add(second)
    db.commit()

    body = client.get(_THRESHOLD_URL, headers=_auth(admin_token)).json()
    assert body["value"] == 0.4
    assert body["embedding_model"] == "emb-b"
    assert body["calibrated_with_embedding_model"] == "emb-a"
    assert body["calibrated"] is False


def test_generic_settings_write_refreshes_calibration_stamp(db):
    """設定頁走 set_setting 時，不可以沿用舊校準時間替新數字背書。"""
    from app.models.model_registry import ModelRegistry

    emb = ModelRegistry(
        name="emb-a",
        display_name="emb-a",
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=True,
        embedding_native_dim=8,
    )
    db.add(emb)
    db.flush()
    set_kb_threshold(db, 0.4, embedding_model="emb-a")
    stamp = db.get(PlatformSetting, KB_THRESHOLD_CALIBRATED_AT_KEY)
    assert stamp is not None
    stamp.value = "2020-01-01T00:00:00+00:00"
    db.flush()

    set_setting(db, KB_THRESHOLD_KEY, 0.9)
    db.flush()
    db.expire_all()
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    assert row is not None and float(row.value) == 0.9
    assert db.get(PlatformSetting, KB_THRESHOLD_EMBEDDING_KEY).value == "emb-a"
    assert db.get(PlatformSetting, KB_THRESHOLD_CALIBRATED_AT_KEY).value != "2020-01-01T00:00:00+00:00"
