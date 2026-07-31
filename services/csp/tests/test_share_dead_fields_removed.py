# -*- coding: utf-8 -*-
"""分享的 ``mode`` / ``allow_fork`` 已從 API 契約移除。

專案規則:收下一個欄位然後把它丟掉,就該把欄位拿掉,而不是留著。
這兩個欄位從來沒有被任何授權判定讀過(分享一律唯讀),留著只會讓
呼叫端以為自己設定了權限 —— 那正是「靜默成功」那類最貴的缺陷。

⚠ 資料庫欄位仍在(DROP COLUMN 要 migration),見 models/conversation.py
的註解與本包報告。這裡守的是 API 契約那一半。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.conversations import ShareCreate, ShareOut

from tests.test_p43_named_shares import _bearer, _make_conv
from tests.conftest import make_user


class TestDeadShareFieldsAreGoneFromTheContract:
    def test_share_schemas_no_longer_declare_them(self):
        assert "mode" not in ShareCreate.model_fields
        assert "allow_fork" not in ShareCreate.model_fields
        assert "mode" not in ShareOut.model_fields
        assert "allow_fork" not in ShareOut.model_fields

    def test_response_body_omits_them(self, client: TestClient, db: Session):
        owner = make_user(db, username="deadfield_owner")
        target = make_user(db, username="deadfield_target")
        conv = _make_conv(db, owner, "無機密")

        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={"target_username": target.username},
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "mode" not in body
        assert "allow_fork" not in body

    def test_legacy_client_still_sending_them_is_not_rejected(
        self, client: TestClient, db: Session
    ):
        """舊前端(apps/anila-shell)仍會送這兩個鍵。Pydantic 預設
        ``extra='ignore'``,所以必須是「忽略」而不是 422 —— 這包不准
        把還在線上的前端打掛。"""
        owner = make_user(db, username="legacy_owner")
        target = make_user(db, username="legacy_target")
        conv = _make_conv(db, owner, "無機密")

        resp = client.post(
            f"/api/conversations/{conv.id}/shares",
            headers=_bearer(owner),
            json={
                "target_username": target.username,
                "mode": "fork",
                "allow_fork": True,
            },
        )

        assert resp.status_code == 201, resp.text
        assert "allow_fork" not in resp.json()
