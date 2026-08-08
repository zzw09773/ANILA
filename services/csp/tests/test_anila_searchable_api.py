"""ANILA 檢索標記端點：管理員限定 ＋ 四道前置檢查，每一次拒絕都要給做法。

AC:
1. 標記是密等相鄰操作 → 只有 admin 動得了（非 admin 403）
2. 本庫密等不是無機密 → 拒絕，指名該密等並指向降密申請流程（四級逐一驗）
3. 個人知識庫（origin=anilalm）不可標記 → 出路是「另建一個庫重新上傳」
4. 嵌入模型與「已標記集」不同 → 拒絕，指名兩邊的模型；出路是「用已標記集
   那個模型另建一個庫、重新上傳」
5. 庫內有非無機密文件 → 拒絕，並指名那個密等
6. 已標記的庫要升密 → 撞到 DB CHECK 時翻譯成看得懂的 400，指向「先取消標記」，
   而且整批回復（庫與庫內文件的密等都不動）
7. 取消標記永遠放行，兩個方向都留稽核

⚠ 這一包的驗收標準不是「回了 400」，也不是「訊息裡有出現做法」，而是
**照著那個做法走真的拿得到想要的東西**。第 4 條踩過一次：原本寫「請先重新
嵌入再標記」，但本平台根本沒有 reindex（`search.py:639` 已經裁定過，並明文
禁止把它寫進使用者訊息），PATCH 也不收 embedding_model（送了回 200、什麼
都沒發生）。那是一扇畫在牆上的門，比直接說「不行」更糟——照做的人會以為
是自己弄錯了。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.audit_log import AuditLog
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.schemas.contracts.classification import ClassificationLevel
from app.services.audit_service import parse_metadata
from tests.conftest import login, make_user

_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.to_storage()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_payload(
    name: str,
    *,
    level: str = _UNCLASSIFIED,
    origin: str = "csp",
    embedding_model: str | None = None,
) -> dict:
    payload = {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "classification_level": level,
        "origin": origin,
    }
    if embedding_model is not None:
        payload["embedding_model"] = embedding_model
    return payload


def _create_collection(client, token: str, name: str, **kwargs) -> dict:
    resp = client.post(
        "/api/ingestion/collections",
        headers=_auth(token),
        json=_create_payload(name, **kwargs),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_document(
    db, collection_id: int, *, classification_level: str = _UNCLASSIFIED
) -> IngestionDocument:
    """庫內塞一份文件。sha256 依 collection 內既有份數變化——
    ``uq_documents_collection_sha256`` 會擋掉同庫重複值。"""
    seq = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == collection_id)
        .count()
    )
    doc = IngestionDocument(
        collection_id=collection_id,
        filename=f"doc-{seq}.txt",
        sha256=f"{seq:064d}",
        status="pending",
        classification_level=classification_level,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture()
def admin_token(client, db) -> str:
    """function-scoped：每支測試各自的 admin，避免共用列被上一支測試改壞。"""
    make_user(db, username="mark_admin", role="admin")
    return login(client, "mark_admin")


def test_non_admin_cannot_mark(client, db):
    """標記是密等相鄰操作,只有 admin 能動。"""
    make_user(db, username="plain_user", role="developer")
    token = login(client, "plain_user")
    coll = _create_collection(client, token, "一般庫")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(token))
    assert r.status_code == 403


def test_cannot_mark_an_anilalm_personal_notebook(client, db, admin_token):
    """個人知識庫變全院可搜不是本功能的本意。"""
    coll = _create_collection(client, admin_token, "我的筆記", origin="anilalm")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "個人知識庫" in r.json()["detail"]


def test_cannot_mark_when_embedding_model_differs_from_the_marked_set(
    client, db, admin_token
):
    """不同嵌入空間的分數不能互比;錯誤訊息要給做法,不能只說不行。"""
    a = _create_collection(client, admin_token, "甲", embedding_model="nv-embed-v2")
    client.patch(f"/api/ingestion/collections/{a['id']}",
                 json={"anila_searchable": True}, headers=_auth(admin_token))
    b = _create_collection(client, admin_token, "乙", embedding_model="other-model")
    r = client.patch(f"/api/ingestion/collections/{b['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "other-model" in detail and "nv-embed-v2" in detail
    # 給做法,不是只說不行——而且必須是走得通的那一條。
    assert "另建" in detail and "重新上傳" in detail
    # 不可以再指回「重新嵌入這個庫」：本平台沒有 reindex（search.py:639）。
    assert "重新嵌入" not in detail, detail


def test_the_embedding_remedy_actually_opens_the_door(client, db, admin_token):
    """走一遍訊息叫人做的事，確認那扇門真的開得了。

    這一支是本輪修正的核心：上一版訊息寫的是「請先重新嵌入再標記」，而
    `PATCH {"embedding_model": ...}` 回 200 卻什麼都沒改、平台也沒有 reindex
    ——訊息指的是一扇畫在牆上的門。所以這裡不只驗訊息內容，直接照做：
    用已標記集的模型另建一個庫，然後標記，必須成功。
    """
    a = _create_collection(client, admin_token, "甲庫", embedding_model="nv-embed-v2")
    assert client.patch(f"/api/ingestion/collections/{a['id']}",
                        json={"anila_searchable": True},
                        headers=_auth(admin_token)).status_code == 200

    b = _create_collection(client, admin_token, "乙庫", embedding_model="other-model")
    refused = client.patch(f"/api/ingestion/collections/{b['id']}",
                           json={"anila_searchable": True}, headers=_auth(admin_token))
    assert refused.status_code == 400

    # 訊息裡沒說、實際上也做不到的那條路：改 embedding_model。回 200，
    # 但模型原封不動——這正是不能把它寫進出路的理由。
    patched = client.patch(f"/api/ingestion/collections/{b['id']}",
                           json={"embedding_model": "nv-embed-v2"},
                           headers=_auth(admin_token))
    assert patched.status_code == 200, patched.text
    assert patched.json()["embedding_model"] == "other-model"
    assert client.patch(f"/api/ingestion/collections/{b['id']}",
                        json={"anila_searchable": True},
                        headers=_auth(admin_token)).status_code == 400

    # 訊息真正叫人做的那條路：用已標記集的模型另建一個庫、重新上傳。
    c = _create_collection(client, admin_token, "丙庫", embedding_model="nv-embed-v2")
    _add_document(db, c["id"])
    ok = client.patch(f"/api/ingestion/collections/{c['id']}",
                      json={"anila_searchable": True}, headers=_auth(admin_token))
    assert ok.status_code == 200, ok.text
    assert ok.json()["anila_searchable"] is True


def test_cannot_mark_when_the_library_holds_a_classified_document(
    client, db, admin_token
):
    """CHECK 只鎖 collection 那一列,管不到文件 —— 標記時先把話講明。"""
    coll = _create_collection(client, admin_token, "混雜庫")
    _add_document(db, coll["id"], classification_level="機密")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "機密" in r.json()["detail"]


def test_raising_classification_while_marked_says_what_to_do(
    client, db, admin_token
):
    """撞到 CHECK 的人要看得懂、知道怎麼自救,不是 500。

    ⚠ 與 task-2-brief 的差異（唯一一處）：brief 寫的是對 PATCH 送
    ``classification_level``。那條路被 `test_collection_classification_raise.py`
    :229 釘死成 **422**（PATCH 一律不得改密等，才不會繞過綁定 agent 檢查與
    文件級聯），該裁決先於本任務。升密的唯一入口是
    ``POST /api/ingestion/collections/{id}/classification``，CHECK 也只可能在
    那裡撞到（app/api/ingestion/collections.py:446 是 collection 密等的唯一寫入點），
    所以斷言原封不動搬到那條路上。
    """
    coll = _create_collection(client, admin_token, "要升密的庫")
    client.patch(f"/api/ingestion/collections/{coll['id']}",
                 json={"anila_searchable": True}, headers=_auth(admin_token))
    # 庫內先放一份文件：升密會先級聯文件、最後才撞到 collection 那一列的
    # CHECK，所以「已整批回復」這句話有沒有兌現，要看這份文件退不退得回來。
    doc = _add_document(db, coll["id"])
    r = client.post(f"/api/ingestion/collections/{coll['id']}/classification",
                    json={"classification_level": "機密"}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "取消" in r.json()["detail"] and "ANILA" in r.json()["detail"]

    # 整批回復：密等沒動、文件也沒被留在半路、標記沒被順手關掉。
    row = db.get(IngestionCollection, coll["id"])
    db.refresh(row)
    assert row.classification_level == _UNCLASSIFIED
    assert row.anila_searchable is True
    db.refresh(doc)
    assert doc.classification_level == _UNCLASSIFIED


# ── brief 未列、但同一條「拒絕要給做法」原則要求的兩支 ────────────────────


def test_cannot_mark_a_classified_library_and_says_where_to_go(
    client, db, admin_token
):
    """Task 1 的 CHECK 讓密等庫「標記不起來」——API 要說人話，不是回
    「conflicts with an existing collection」那種 409。

    四級逐一驗，避免只擋最高級（黑名單式防護的老毛病）。
    """
    for level in ClassificationLevel:
        if level is ClassificationLevel.UNCLASSIFIED:
            continue
        coll = _create_collection(
            client, admin_token, f"密等庫-{level.name}", level=level.to_storage()
        )
        r = client.patch(
            f"/api/ingestion/collections/{coll['id']}",
            json={"anila_searchable": True},
            headers=_auth(admin_token),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert level.to_storage() in detail
        assert "降密" in detail, detail  # 給做法：走降密流程

        row = db.get(IngestionCollection, coll["id"])
        db.refresh(row)
        assert row.anila_searchable is False


def test_unmarking_is_never_blocked_by_the_guards(client, db, admin_token):
    """關標記不受四道檢查限制——那四則訊息叫人做的就是這個動作。

    情境：庫標記完之後才被塞進一份密等文件（第 4 道檢查現在會擋）。此時
    管理員唯一的自救動作就是關標記，如果連關都被同一道檢查擋住，使用者就
    被鎖在裡面了。
    """
    coll = _create_collection(client, admin_token, "後來變髒的庫")
    on = client.patch(f"/api/ingestion/collections/{coll['id']}",
                      json={"anila_searchable": True}, headers=_auth(admin_token))
    assert on.status_code == 200, on.text

    _add_document(db, coll["id"], classification_level="機密")

    off = client.patch(f"/api/ingestion/collections/{coll['id']}",
                       json={"anila_searchable": False}, headers=_auth(admin_token))
    assert off.status_code == 200, off.text
    assert off.json()["anila_searchable"] is False
    row = db.get(IngestionCollection, coll["id"])
    db.refresh(row)
    assert row.anila_searchable is False


def test_mark_round_trips_and_writes_audit(client, db, admin_token):
    """成功的那一條路也要驗：回應看得到、DB 真的變了、稽核留痕、關得掉。

    「按了沒報錯但什麼也沒發生」是本專案列名的最糟失敗模式，所以 200 本身
    不算通過——要看到欄位真的翻面。
    """
    coll = _create_collection(client, admin_token, "院級法規庫")
    assert coll["anila_searchable"] is False

    on = client.patch(f"/api/ingestion/collections/{coll['id']}",
                      json={"anila_searchable": True}, headers=_auth(admin_token))
    assert on.status_code == 200, on.text
    assert on.json()["anila_searchable"] is True
    row = db.get(IngestionCollection, coll["id"])
    db.refresh(row)
    assert row.anila_searchable is True

    def _mark_audits() -> list:
        return [
            a
            for a in db.query(AuditLog)
            .filter(AuditLog.action == "ingestion_collection_anila_searchable_set")
            .order_by(AuditLog.id)
            .all()
            if str(getattr(a, "resource_id", "")) == str(coll["id"])
        ]

    audits = _mark_audits()
    assert audits, "標記翻面必須留稽核"
    assert parse_metadata(audits[-1].metadata_json)["to"] is True

    # 關得掉——這是每一則拒絕訊息叫人去做的那個動作。
    off = client.patch(f"/api/ingestion/collections/{coll['id']}",
                       json={"anila_searchable": False}, headers=_auth(admin_token))
    assert off.status_code == 200, off.text
    assert off.json()["anila_searchable"] is False
    db.refresh(row)
    assert row.anila_searchable is False

    # 關的方向也要留稽核。全院檢索範圍「縮小」跟「擴大」一樣是要能查的事：
    # 只記開不記關，稽核帳上會永遠停在「這個庫是開著的」。
    audits = _mark_audits()
    assert len(audits) == 2, [parse_metadata(a.metadata_json) for a in audits]
    closing = parse_metadata(audits[-1].metadata_json)
    assert closing["from"] is True and closing["to"] is False
