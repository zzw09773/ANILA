# -*- coding: utf-8 -*-
"""受控對話的後端外流面 gate —— 補救計畫 W1-1 ①。

缺口
----
五級分類是 `無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密`,而 legacy 的
`classified` boolean 的鏡射規則是 **`classified = level >= 機密`**
(`api/conversations.py:125` 自己寫明)。也就是說**營業秘密的 `classified` 是
False**。

而後端有兩個外流面 gate 拿這個 boolean 當授權輸入:

    api/conversations.py:309   if not c.classified:   → 搜尋結果要不要附內文片段
    api/conversations.py:333   if conv.classified:    → 讀取要不要寫稽核列

於是**營業秘密對話在這兩個面上等同無機密**:

1. 搜尋會把命中的訊息內文擷取 80 字回傳 —— 內容外洩,而且是在「列表」這種
   最容易被截圖、最不會被注意的地方。
2. 讀取不留任何稽核紀錄 —— 事後查不出誰看過。這對營業秘密尤其致命:降密要
   兩個人加一份公文文號,而讀取連一列 log 都沒有。

判定必須 fail-closed:未知/損壞的儲存值視同**受控**,而不是預設無機密。這是
`conversation_service.is_publicly_shareable` 已經建立的姿態,本包把它抽成
共用的 `is_controlled()`,讓五個外流面共用同一個定義。

**淨退化陷阱(計畫的風險欄點名)**:門檻若寫成「絕對機密」或沿用「>= 機密」,
就是重演第一輪的錯誤。所以下面每一條都用**營業秘密**當案例釘死。
"""
from __future__ import annotations

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.audit_log import AuditLog
from tests.conftest import login, make_user


LEVELS_CONTROLLED = ["營業秘密", "機密", "極機密", "絕對機密"]


def _mk_conv(db, user, level: str, *, title="季度營收與客戶名單") -> Conversation:
    conv = Conversation(user_id=user.id, title=title, classification_level=level)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _mk_msg(db, conv, content: str) -> Message:
    msg = Message(conversation_id=conv.id, role="user", content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _audit_rows(db, conv_id: int):
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "conversation",
            AuditLog.resource_id == str(conv_id),
        )
        .all()
    )


# ── gate ①-a:讀取受控對話必須落稽核列 ────────────────────────────────────────

def test_reading_a_trade_secret_conversation_is_audited(client, db):
    """營業秘密 —— legacy boolean 是 False,所以舊碼一列都不寫。"""
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "營業秘密")

    token = login(client, username="alice")
    resp = client.get(
        f"/api/conversations/{conv.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    rows = _audit_rows(db, conv.id)
    assert len(rows) == 1, "營業秘密對話被讀取卻沒有稽核列"
    assert rows[0].actor_user_id == user.id
    # 稽核列必須記下**實際密等**,不能只寫「classified」——事後要能分辨
    # 這是營業秘密還是絕對機密,否則稽核只剩「有人看過某個受控東西」。
    assert "營業秘密" in (rows[0].detail or "")


def test_every_controlled_level_is_audited(client, db):
    """四個受控等級都要落列,不只 >= 機密 的那兩個。"""
    for i, level in enumerate(LEVELS_CONTROLLED):
        user = make_user(db, username=f"u{i}")
        conv = _mk_conv(db, user, level)
        token = login(client, username=f"u{i}")
        client.get(
            f"/api/conversations/{conv.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert len(_audit_rows(db, conv.id)) == 1, f"{level} 沒落稽核列"


def test_unclassified_read_is_not_audited(client, db):
    """無機密不落列 —— 否則稽核被稀釋成雜訊(W1-5 的治理可用性問題)。"""
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    token = login(client, username="alice")
    client.get(
        f"/api/conversations/{conv.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert _audit_rows(db, conv.id) == []


def test_unknown_classification_fails_closed_to_audited(client, db):
    """損壞/未知的儲存值視同受控 —— 不是預設無機密。"""
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    # 繞過 API 直接把欄位寫成非法值(模擬資料損壞或未來新增的等級)
    db.query(Conversation).filter(Conversation.id == conv.id).update(
        {"classification_level": "他媽的什麼等級"}
    )
    db.commit()

    token = login(client, username="alice")
    resp = client.get(
        f"/api/conversations/{conv.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(_audit_rows(db, conv.id)) == 1, "未知密等沒有 fail-closed"


# ── gate ①-b:搜尋不得回傳受控對話的內文片段 ────────────────────────────────

def test_search_does_not_leak_trade_secret_message_content(client, db):
    secret = "客戶 A 的授權金報價是每年三百二十萬"
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "營業秘密")
    _mk_msg(db, conv, secret)

    token = login(client, username="alice")
    resp = client.get(
        "/api/conversations/search",
        params={"q": "授權金"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    hits = [h for h in resp.json() if h["id"] == conv.id]
    assert hits, "對話本身仍應出現在搜尋結果(只是不附內文)"
    assert hits[0]["snippet"] is None, f"營業秘密內文被搜尋洩漏:{hits[0]['snippet']!r}"


def test_search_still_returns_snippet_for_unclassified(client, db):
    """不得誤擋 —— 無機密的片段功能要照舊。"""
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密", title="午餐吃什麼")
    _mk_msg(db, conv, "今天想吃牛肉麵配滷味")

    token = login(client, username="alice")
    resp = client.get(
        "/api/conversations/search",
        params={"q": "牛肉麵"},
        headers={"Authorization": f"Bearer {token}"},
    )
    hits = [h for h in resp.json() if h["id"] == conv.id]
    assert hits and hits[0]["snippet"], "無機密的搜尋片段被誤擋"
    assert "牛肉麵" in hits[0]["snippet"]


def test_search_snippet_suppressed_for_every_controlled_level(client, db):
    user = make_user(db, username="alice")
    token = login(client, username="alice")
    for level in LEVELS_CONTROLLED:
        conv = _mk_conv(db, user, level, title=f"專案-{level}")
        _mk_msg(db, conv, f"這是 {level} 的獨特內文 xyzzy{level}")
        resp = client.get(
            "/api/conversations/search",
            params={"q": "xyzzy"},
            headers={"Authorization": f"Bearer {token}"},
        )
        hits = [h for h in resp.json() if h["id"] == conv.id]
        assert hits, f"{level} 的對話從搜尋結果消失了(應該只擋片段)"
        assert hits[0]["snippet"] is None, f"{level} 的內文被洩漏"


# ── 共用判定的直接單元測試 ───────────────────────────────────────────────────

def test_is_controlled_predicate():
    from app.services.conversation_service import is_controlled, is_publicly_shareable

    class Fake:
        def __init__(self, level):
            self.classification_level = level

    assert is_controlled(Fake("營業秘密")) is True
    assert is_controlled(Fake("機密")) is True
    assert is_controlled(Fake("極機密")) is True
    assert is_controlled(Fake("絕對機密")) is True
    assert is_controlled(Fake("無機密")) is False
    # fail-closed
    assert is_controlled(Fake(None)) is True
    assert is_controlled(Fake("")) is True
    assert is_controlled(Fake("nonsense")) is True

    # 兩個判定必須是同一個定義的正反面,不能各自漂移
    for level in ("無機密", "營業秘密", "機密", "極機密", "絕對機密", None, "nonsense"):
        assert is_publicly_shareable(Fake(level)) is not is_controlled(Fake(level))
