# -*- coding: utf-8 -*-
"""Launch Gateway —— 同源相對 entry_url、停用服務、ANILA LM release gate。

背景(2026-08-02 實測):shell「專案入口」六張服務卡有五張按了什麼也不會
發生。五個自營服務的 ``entry_url`` 是相對路徑(``/anila``、``/n8n``…),
launch 端點卻硬性要求絕對 http(s) URL → 400。相對路徑才是對的資料(同一台
nginx 同源代理,寫死主機名等於兩份會靜默分歧的設定),要修的是端點。

這一份守住四件事:
  1. 相對 entry_url 可啟動,且 launch token 進的是 query 而不是 path
     (曾經被 nginx 的補斜線 301 接成 ``?launch_token=x/`` 無限重導)。
  2. 跨主機服務的 ``allowed_origins`` 白名單一格都沒放寬。
  3. 停用的服務不可啟動。
  4. release gate 關著時,ANILA LM 不出現在任何「可用服務」清單、不可啟動,
     但管理員仍看得到、管得動那筆註冊。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from urllib.parse import parse_qs, urlparse

import pytest

from app.models.registered_service import RegisteredService
from app.services import anilalm_release_gate as release_gate
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _make_service(db, *, name, slug, entry_url, **kw) -> RegisteredService:
    svc = RegisteredService(name=name, slug=slug, entry_url=entry_url, **kw)
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def _headers(client, db, username="alice", role="user") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


# ── 1. 同源相對 entry_url ────────────────────────────────────────────────────


class TestSameOriginRelativeEntryUrl:
    def test_relative_entry_url_launches(self, client, db):
        """``/anila`` 這種同源絕對路徑要能啟動(修正前:400)。"""
        headers = _headers(client, db)
        svc = _make_service(
            db, name="ANILA", slug="anila", entry_url="/anila", is_public=True
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["launch_url"].startswith("/anila?launch_token=")

    def test_launch_token_lands_in_query_not_path(self, client, db):
        """launch token 必須是**查詢參數**,path 一個字都不能被動到。

        這條釘的是 2026-08-01 的無限重導:nginx 補尾斜線時把斜線接在
        query 之後(``?launch_token=x`` → ``?launch_token=x/`` → ``x//////``)。
        nginx 那邊已經修好,這裡守的是「我們發出去的 URL 本身是對的」——
        就算 nginx 又壞掉,壞的也只有 nginx 那一段。
        """
        headers = _headers(client, db)
        svc = _make_service(
            db, name="n8n", slug="n8n", entry_url="/n8n", is_public=True
        )
        body = client.post(
            f"/api/services/{svc.slug}/launch", json={}, headers=headers
        ).json()
        parts = urlparse(body["launch_url"])
        assert parts.scheme == "" and parts.netloc == ""  # 仍是同源相對路徑
        assert parts.path == "/n8n"  # path 未被污染,結尾不是 token
        assert parse_qs(parts.query)["launch_token"] == [body["launch_token"]]
        assert not parts.fragment

    def test_relative_entry_url_with_existing_query_is_preserved(self, client, db):
        headers = _headers(client, db)
        svc = _make_service(
            db, name="Deep", slug="deep", entry_url="/svc?tab=1", is_public=True
        )
        body = client.post(
            f"/api/services/{svc.slug}/launch", json={}, headers=headers
        ).json()
        q = parse_qs(urlparse(body["launch_url"]).query)
        assert q["tab"] == ["1"]
        assert q["launch_token"] == [body["launch_token"]]

    @pytest.mark.parametrize(
        "entry_url",
        [
            "//evil.example/x",  # 協定相對 ⇒ 另一個 origin
            "/\\evil.example/x",  # 瀏覽器在 authority 位置把 \ 折成 /
            "/\t/evil.example/x",  # WHATWG 解析前會刪 tab ⇒ 等同 //evil…
            "anila",  # 相對參照,解析結果取決於當下頁面
            "ftp://x/y",  # 非 http(s)
        ],
    )
    def test_non_same_origin_lookalikes_still_refused(self, client, db, entry_url):
        """看起來像同源、實際會跑到別的主機的寫法,一律照舊 400。"""
        headers = _headers(client, db)
        svc = _make_service(
            db, name="壞資料", slug="bad-url", entry_url=entry_url, is_public=True
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "http(s)" in resp.json()["detail"]


# ── 2. 跨主機白名單沒被放寬 ──────────────────────────────────────────────────


class TestCrossHostAllowlistUnchanged:
    def test_cross_host_within_allowlist_launches(self, client, db):
        """MLSteam 型(真跨主機 + 白名單命中)照舊可用。"""
        headers = _headers(client, db)
        svc = _make_service(
            db,
            name="MLSteam",
            slug="mlsteam",
            entry_url="https://aiops.example.org:4443/",
            allowed_origins=["https://aiops.example.org:4443"],
            is_public=True,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["launch_url"].startswith(
            "https://aiops.example.org:4443/?launch_token="
        )

    def test_cross_host_outside_allowlist_refused(self, client, db):
        headers = _headers(client, db)
        svc = _make_service(
            db,
            name="被改過的服務",
            slug="tampered",
            entry_url="https://attacker.example/steal",
            allowed_origins=["https://aiops.example.org:4443"],
            is_public=True,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "服務 entry_url origin 不在 allowed_origins"

    def test_relative_entry_url_does_not_satisfy_a_cross_host_allowlist(
        self, client, db
    ):
        """同源 entry_url + 非空白名單 = 自相矛盾的設定,大聲擋掉,不靜默忽略。"""
        headers = _headers(client, db)
        svc = _make_service(
            db,
            name="設定打架",
            slug="conflicting",
            entry_url="/anila",
            allowed_origins=["https://aiops.example.org:4443"],
            is_public=True,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "服務 entry_url origin 不在 allowed_origins"


# ── 3. 停用的服務不可啟動 ────────────────────────────────────────────────────


class TestInactiveServiceCannotLaunch:
    def test_disabled_service_refused_for_regular_user_as_404(self, client, db):
        headers = _headers(client, db, username="carol")
        svc = _make_service(
            db,
            name="已停用",
            slug="retired",
            entry_url="/retired",
            is_public=True,
            is_active=False,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 404
        # 與「id 不存在」完全同形:不做存在性 oracle。
        assert resp.json()["detail"] == "服務不存在"
        missing = client.post(
            "/api/services/no-such-service/launch", json={}, headers=headers
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == resp.json()["detail"]

    def test_disabled_service_refused_for_admin_with_actionable_detail(
        self, client, db
    ):
        """admin 本來就看得到整份註冊表,告訴他「已停用」不多洩漏任何東西,
        卻是他唯一能自救的訊息。"""
        headers = _headers(client, db, username="root", role="admin")
        svc = _make_service(
            db,
            name="已停用",
            slug="retired",
            entry_url="/retired",
            is_public=True,
            is_active=False,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 409, resp.text
        assert "已停用" in resp.json()["detail"]

    def test_disabled_service_issues_no_launch_row_or_token(self, client, db):
        from app.models.service_launch import ServiceLaunch

        headers = _headers(client, db, username="dave")
        svc = _make_service(
            db,
            name="已停用",
            slug="retired",
            entry_url="/retired",
            is_public=True,
            is_active=False,
        )
        client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert db.query(ServiceLaunch).filter_by(service_id=svc.id).count() == 0


# ── 4. ANILA LM release gate ────────────────────────────────────────────────


def _make_anilalm(db, **kw) -> RegisteredService:
    return _make_service(
        db,
        name="ANILA LM",
        slug="anila-lm",
        entry_url="/anilalm",
        is_public=True,
        **kw,
    )


class TestAnilaLmReleaseGate:
    def test_gate_is_closed_in_this_release(self):
        assert release_gate.ANILA_LM_RELEASED is False

    def test_absent_from_user_facing_service_list(self, client, db):
        headers = _headers(client, db, username="erin")
        _make_anilalm(db)
        _make_service(
            db, name="ANILA", slug="anila", entry_url="/anila", is_public=True
        )
        rows = client.get("/api/services", headers=headers).json()
        names = [r["name"] for r in rows]
        assert "ANILA" in names
        assert "ANILA LM" not in names

    def test_absent_from_platform_links(self, client, db):
        headers = _headers(client, db, username="frank")
        _make_anilalm(db)
        rows = client.get("/api/platform-links", headers=headers).json()
        assert [r for r in rows if r["name"] == "ANILA LM"] == []

    def test_absent_from_admins_default_list_too(self, client, db):
        """admin 在 shell 也是使用者;預設清單就是使用者面,不能對 admin 破例。"""
        headers = _headers(client, db, username="root", role="admin")
        _make_anilalm(db)
        rows = client.get("/api/services", headers=headers).json()
        assert [r for r in rows if r["name"] == "ANILA LM"] == []

    def test_admin_can_still_see_and_manage_the_registration(self, client, db):
        """閘門關的是「可用」,不是「可管理」——管理清單必須看得到,
        而且編輯 / 停用真的打得進去。"""
        headers = _headers(client, db, username="root", role="admin")
        svc = _make_anilalm(db)

        listed = client.get(
            "/api/services?include_inactive=true", headers=headers
        ).json()
        assert [r["name"] for r in listed if r["name"] == "ANILA LM"] == ["ANILA LM"]
        links = client.get(
            "/api/platform-links?include_inactive=true", headers=headers
        ).json()
        assert [r["name"] for r in links if r["name"] == "ANILA LM"] == ["ANILA LM"]

        edited = client.put(
            f"/api/services/{svc.id}",
            json={"description": "整備中"},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["description"] == "整備中"

        disabled = client.delete(f"/api/services/{svc.id}", headers=headers)
        assert disabled.status_code == 200, disabled.text

    def test_launch_refused_while_gate_is_closed(self, client, db):
        """⚠ 這條不靠 is_active —— 全新資料庫的種子會把它建成啟用。"""
        headers = _headers(client, db, username="gail")
        svc = _make_anilalm(db)
        assert svc.is_active is True
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == release_gate.GATED_LAUNCH_DETAIL

    def test_launch_refused_for_admin_too(self, client, db):
        headers = _headers(client, db, username="root", role="admin")
        svc = _make_anilalm(db)
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 503, resp.text

    def test_gate_reopens_cleanly(self, client, db, monkeypatch):
        """把旗標翻成 True 就整組回來 —— 重開不必考古。"""
        monkeypatch.setattr(release_gate, "ANILA_LM_RELEASED", True)
        headers = _headers(client, db, username="hank")
        svc = _make_anilalm(db)
        rows = client.get("/api/services", headers=headers).json()
        assert [r["name"] for r in rows if r["name"] == "ANILA LM"] == ["ANILA LM"]
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["launch_url"].startswith("/anilalm?launch_token=")


class TestReleaseGateMatcher:
    """比對規則與 anilalmReleaseGate.js 的 isAnilaLmPlatformLink 對齊。"""

    class _Row:
        def __init__(self, name="", slug="", entry_url="", env_seed_key=""):
            self.name = name
            self.slug = slug
            self.entry_url = entry_url
            self.env_seed_key = env_seed_key

    @pytest.mark.parametrize(
        "row",
        [
            _Row(name="ANILA LM"),
            _Row(name="anilalm"),
            _Row(slug="anila-lm"),
            _Row(env_seed_key="ANILA LM"),
            _Row(name="改過名字的", entry_url="/anilalm"),
            _Row(name="改過名字的", entry_url="/anilalm/"),
            _Row(name="改過名字的", entry_url="/ANILALM/studio?taskId=3"),
            _Row(name="改過名字的", entry_url="https://host/anilalm"),
        ],
    )
    def test_matches(self, row):
        assert release_gate.is_anila_lm_service(row) is True

    @pytest.mark.parametrize(
        "row",
        [
            _Row(name="ANILA", entry_url="/anila"),
            _Row(name="n8n 工作流程", entry_url="/n8n"),
            _Row(name="MLSteam", entry_url="https://aiops.example.org:4443/"),
            _Row(),
        ],
    )
    def test_does_not_match_other_services(self, row):
        assert release_gate.is_anila_lm_service(row) is False

    def test_none_is_not_a_match(self):
        assert release_gate.is_anila_lm_service(None) is False
