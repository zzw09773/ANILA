# -*- coding: utf-8 -*-
"""對話匯出的落列與密等頁首 —— 補救計畫 W1-1 ④(含 flag 解耦)。

缺口
----
`app.jsx` 的 `exportConversation` **完全沒有分類 gate、沒有密等頁首、也不落
稽核列**。一份離開平台的檔案於是三件事都不成立:

1. 沒有人知道它是什麼密等 —— 收檔者無從判斷自己的處理義務。
2. 沒有人知道是誰在什麼時候帶出去的 —— 外流溯源的第一個問題就答不出來。
3. `export_records` 一列都沒有 —— 稽核面完全空白。

改法是新增 `POST /api/conversations/{id}/export-record`:前端**先落列成功才
產檔**,失敗則擋(斷線 = 不放行)。

節奏解耦(計畫明寫)
--------------------
④是 migration + 新端點,與②③⑤⑥的前端文案不同節奏,所以用
`ANILA_EXPORT_RECORD_REQUIRED`(**預設 off**)包起來:flag off 時前端的三個
gate、密等頁首與 print stylesheet 可以先上,不會被④卡住;flag on 是 Wave 1
的離開條件。

淨退化陷阱
----------
下面用**營業秘密**當主案例。落列的 `classification_level` 必須是**真實密等**,
不能從 legacy `classified` boolean 推導 —— 那個 boolean 對營業秘密是 False,
推導出來會寫成「無機密」,於是稽核紀錄自己說謊。

端點是紀錄面,不是授權面
------------------------
端點對受控對話**不回 403**:它照樣落列(`decision="deny"`),並在回應裡告訴
前端 `allowed=false`。理由是「有人試圖匯出營業秘密對話」正是稽核最想看到的
事件,而 403 會讓它連紀錄都沒有。授權面在前端三個 gate(W1-1②)與未來的
policy engine。
"""
from __future__ import annotations

from app.config import settings
from app.models.artifact import ExportRecord
from app.models.conversation import Conversation
from tests.conftest import login, make_user


LEVELS_CONTROLLED = ["營業秘密", "機密", "極機密", "絕對機密"]


def _mk_conv(db, user, level: str, *, title="季度營收與客戶名單") -> Conversation:
    conv = Conversation(user_id=user.id, title=title, classification_level=level)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _post(client, token, conv_id, fmt="markdown"):
    return client.post(
        f"/api/conversations/{conv_id}/export-record",
        json={"format": fmt},
        headers={"Authorization": f"Bearer {token}"},
    )


def _rows(db, conv_id: int):
    return db.query(ExportRecord).filter(
        ExportRecord.conversation_id == conv_id
    ).all()


# ── flag ON:落列 + 密等頁首 ──────────────────────────────────────────────────

def test_trade_secret_export_is_recorded_with_real_level(client, db, monkeypatch):
    """⑤ 匯出營業秘密 → export_records 落列,且回應含密等頁首字樣。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "營業秘密")
    token = login(client, username="alice")

    resp = _post(client, token, conv.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    rows = _rows(db, conv.id)
    assert len(rows) == 1, "營業秘密的匯出沒有落列"
    row = rows[0]
    # 真實密等,不是從 legacy boolean 推導出來的「無機密」
    assert row.classification_level == "營業秘密"
    assert row.exporter_user_id == user.id
    assert row.export_format == "markdown"
    # artifact 匯出以外的路徑沒有 artifact —— 這正是 artifact_id 要 nullable 的理由
    assert row.artifact_id is None

    # 回應含密等頁首字樣(前端拿它蓋在檔案第一頁)
    assert body["classification_level"] == "營業秘密"
    assert "密等：營業秘密" in "\n".join(body["header_lines"])
    assert "匯出者：alice" in "\n".join(body["header_lines"])
    assert any("匯出時間：" in line for line in body["header_lines"])
    assert any("來源系統：" in line for line in body["header_lines"])
    assert body["recorded"] is True


def test_every_controlled_level_is_recorded_with_its_own_level(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    for i, level in enumerate(LEVELS_CONTROLLED):
        user = make_user(db, username=f"u{i}")
        conv = _mk_conv(db, user, level)
        token = login(client, username=f"u{i}")
        resp = _post(client, token, conv.id, fmt="json")
        assert resp.status_code == 200, resp.text
        rows = _rows(db, conv.id)
        assert len(rows) == 1, f"{level} 沒落列"
        assert rows[0].classification_level == level
        assert f"密等：{level}" in "\n".join(resp.json()["header_lines"])


def test_unclassified_export_is_allowed_and_recorded(client, db, monkeypatch):
    """無機密照舊可匯出(不得誤擋),但仍然留紀錄。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密", title="午餐吃什麼")
    token = login(client, username="alice")

    body = _post(client, token, conv.id).json()
    assert body["allowed"] is True
    assert body["classification_level"] == "無機密"
    assert len(_rows(db, conv.id)) == 1
    assert _rows(db, conv.id)[0].decision == "allow"


def test_controlled_export_is_recorded_as_denied(client, db, monkeypatch):
    """受控對話:allowed=False,而且 decision 落成 deny(紀錄面而非 403)。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "營業秘密")
    token = login(client, username="alice")

    body = _post(client, token, conv.id).json()
    assert body["allowed"] is False
    assert body["blocked_notice"], "被擋時必須給文案(N-3/N-4)"
    assert "營業秘密" in body["blocked_notice"]
    assert _rows(db, conv.id)[0].decision == "deny"


def test_unknown_level_fails_closed_to_denied(client, db, monkeypatch):
    """損壞/未知的密等 fail-closed 視同受控。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    db.query(Conversation).filter(Conversation.id == conv.id).update(
        {"classification_level": "他媽的什麼等級"}
    )
    db.commit()
    token = login(client, username="alice")

    body = _post(client, token, conv.id).json()
    assert body["allowed"] is False
    assert body["classification_level"] == "未知(視同受控)"


def test_stored_level_always_satisfies_the_pg_check_constraint(client, db, monkeypatch):
    """落庫的密等必須是五級字面值 —— 否則 PostgreSQL 的 CHECK 會讓請求 500。

    這條是「測試綠但生產紅」的防線(ledger 的 `sqlite-conftest-vs-pg` 記的同一
    類問題)。PG 上 `export_records.classification_level` 有 CHECK 約束
    (`ck_export_records_classification_level_gate2_level`,r1_0011/r1_0017 建立)
    只允許五級字面值;而測試用的 SQLite schema 由 `Base.metadata.create_all`
    建立、**沒有那個 CHECK**。所以「回應顯示未知(視同受控)」與「落庫寫什麼」
    必須分開驗:回應是自由文字,欄位不是。

    fail-closed 在受限欄位上 = 取最嚴等級,並在 `classification_source` 留
    `:level_unreadable` 後綴,讓報表分辨得出這一列是資料損壞而非真的絕對機密。
    """
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    valid = {"無機密", "營業秘密", "機密", "極機密", "絕對機密"}
    user = make_user(db, username="alice")
    token = login(client, username="alice")

    for i, bad in enumerate(["他媽的什麼等級", "", "TOP SECRET"]):
        conv = _mk_conv(db, user, "無機密", title=f"壞值-{i}")
        db.query(Conversation).filter(Conversation.id == conv.id).update(
            {"classification_level": bad}
        )
        db.commit()
        resp = _post(client, token, conv.id)
        assert resp.status_code == 200, resp.text
        row = _rows(db, conv.id)[0]
        assert row.classification_level in valid, (
            f"落庫密等 {row.classification_level!r} 不在五級內 —— PG 的 CHECK 會擋掉"
        )
        # 最嚴等級 + 可辨識的來源後綴
        assert row.classification_level == "絕對機密"
        assert row.classification_source == "conversation_export:level_unreadable"
        # 回應面仍是誠實的人可讀標籤
        assert resp.json()["classification_level"] == "未知(視同受控)"

    # 正常等級不得被誤標成 unreadable
    ok = _mk_conv(db, user, "營業秘密", title="正常值")
    _post(client, token, ok.id)
    assert _rows(db, ok.id)[0].classification_source == "conversation_export"


def test_taipei_timezone_in_receipt(client, db, monkeypatch):
    """③ 時間一律 Asia/Taipei(UTC+8)。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    token = login(client, username="alice")
    body = _post(client, token, conv.id).json()
    assert body["exported_at"].endswith("+08:00"), body["exported_at"]
    assert any("UTC+8" in line for line in body["header_lines"])


# ── flag OFF:不落列,但頁首照給 ─────────────────────────────────────────────

def test_flag_off_does_not_write_a_row(client, db, monkeypatch):
    """⑦ flag off 時匯出不需要落列 —— 但②③⑤⑥仍正常(頁首照給)。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", False)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "營業秘密")
    token = login(client, username="alice")

    resp = _post(client, token, conv.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] is False
    assert body["required"] is False
    assert _rows(db, conv.id) == [], "flag off 卻落了列 —— flag 沒有生效"
    # ③ 的密等頁首與 flag 無關,必須照給
    assert "密等：營業秘密" in "\n".join(body["header_lines"])
    # ② 的判定也與 flag 無關
    assert body["allowed"] is False


def test_flag_default_is_off():
    """預設必須 off —— ④與②③⑤⑥不同節奏,預設 on 會把五件已完成的工作綁住。"""
    from app.config import Settings

    assert Settings().ANILA_EXPORT_RECORD_REQUIRED is False


# ── 授權邊界 ─────────────────────────────────────────────────────────────────

def test_other_users_conversation_is_rejected(client, db, monkeypatch):
    """別人的對話不得落列 —— 沿用 `_check_access` 既有的 403 姿態。"""
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    owner = make_user(db, username="alice")
    make_user(db, username="bob")
    conv = _mk_conv(db, owner, "無機密")
    token = login(client, username="bob")
    resp = _post(client, token, conv.id)
    assert resp.status_code in (403, 404), resp.text
    assert _rows(db, conv.id) == []


def test_anonymous_is_rejected(client, db):
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    resp = client.post(
        f"/api/conversations/{conv.id}/export-record", json={"format": "markdown"}
    )
    assert resp.status_code in (401, 403), resp.text


def test_bad_format_is_422(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_EXPORT_RECORD_REQUIRED", True)
    user = make_user(db, username="alice")
    conv = _mk_conv(db, user, "無機密")
    token = login(client, username="alice")
    resp = _post(client, token, conv.id, fmt="exe")
    assert resp.status_code == 422, resp.text
    assert _rows(db, conv.id) == []


# ── schema:artifact_id 必須 nullable(r1_0038)───────────────────────────────

def test_export_record_artifact_id_is_nullable():
    """對話匯出沒有 artifact,欄位不放寬就寫不進去(這是④的 migration 理由)。"""
    assert ExportRecord.__table__.c.artifact_id.nullable is True
    assert ExportRecord.__table__.c.conversation_id.nullable is True


def test_migration_r1_0038_exists_and_keeps_single_head():
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    texts = {p.name: p.read_text(encoding="utf-8") for p in versions.glob("*.py")}
    assert any(n.startswith("r1_0038") for n in texts), "缺 r1_0038"

    revisions: set[str] = set()
    downs: set[str] = set()
    rev_re = re.compile(r'^revision(?::[^=]+)?\s*=\s*["\']([^"\']+)["\']', re.M)
    down_re = re.compile(
        r'^down_revision(?::[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', re.M
    )
    for text in texts.values():
        rev = rev_re.search(text)
        if rev:
            revisions.add(rev.group(1))
        down = down_re.search(text)
        if down and down.group(1):
            downs.add(down.group(1))
    heads = revisions - downs
    assert heads == {"r1_0038"}, f"alembic head 應唯一且為 r1_0038,實得 {heads}"
