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
            "/\t/evil.example/x",  # 見下面 TestControlCharStripIsPinned 的說明
            "anila",  # 相對參照,解析結果取決於當下頁面
            "ftp://x/y",  # 非 http(s)
        ],
    )
    def test_non_same_origin_lookalikes_still_refused(self, client, db, entry_url):
        """看起來像同源、實際會跑到別的主機的寫法,一律照舊 400。

        ⚠ 這一組**不是** ``_URL_CONTROL_STRIP`` 的覆蓋。``/\\t/evil.example`` 這
        個形狀就算把正規化整段拿掉也照樣被擋(CPython ≥3.6 的 ``urlsplit`` 自己
        就會刪 tab/CR/LF),所以它證明不了那行程式有在做事。真正釘住正規化的是
        下面那個 class ——「刪掉 ``\\t`` 就要有測試變紅」。
        """
        headers = _headers(client, db)
        svc = _make_service(
            db, name="壞資料", slug="bad-url", entry_url=entry_url, is_public=True
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "http(s)" in resp.json()["detail"]


# ── 1b. 控制字元正規化:每個刪掉的字元都要有自己的紅燈 ────────────────────────

# WHATWG URL Standard 在解析前刪掉的三個字元(tab / LF / CR)。
# ⚠ 刻意**不從** ``_URL_CONTROL_STRIP`` 讀回來:若測試的清單是正式碼那份的投影,
#   從正式碼刪掉 ``\t`` 會連同它的測試案例一起消失,整組照樣全綠 —— 那不叫覆蓋。
#   這份是測試自己的副本,兩份不一致要有人來裁決(見 test_strip_set_is_exactly…)。
_WHATWG_STRIPPED = ("\t", "\n", "\r")


class TestControlCharStripIsPinned:
    """``_normalise_entry_url`` 刪掉的每一個控制字元,都要有一條「拿掉它就變紅」
    的測試。

    為什麼是「控制字元 + 反斜線」這個形狀:CPython 的 ``urlsplit`` 本來就會刪
    tab/CR/LF,所以 ``/<TAB>/host`` 有沒有我們這行都會被擋。**只有控制字元後面
    接反斜線**時,我們手寫的正規化才真的在做事 ——

        輸入 ``/<TAB>\\evil.example``
          有正規化:先刪 tab ⇒ ``/\\evil.example`` ⇒ ``[1:2] == "\\"`` ⇒ 擋下,400。
          沒正規化:``[1:2]`` 是 tab 不是反斜線 ⇒ 過關;而 ``urlsplit`` 又把 tab
                    刪掉,於是判成「同源相對路徑」⇒ 帶著 launch token 發出去。
                    瀏覽器拿到 ``/<TAB>\\evil.example`` 一樣刪 tab、把 ``\\`` 折成
                    ``/`` ⇒ ``//evil.example`` ⇒ **token 送到別人的主機**。
    """

    @pytest.mark.parametrize(
        "ctrl", _WHATWG_STRIPPED, ids=["tab", "lf", "cr"]
    )
    def test_control_char_then_backslash_is_refused(self, client, db, ctrl):
        headers = _headers(client, db)
        svc = _make_service(
            db,
            name="控制字元逃逸",
            slug="ctrl-escape",
            entry_url=f"/{ctrl}\\evil.example",
            is_public=True,
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "http(s)" in resp.json()["detail"]

    def test_strip_set_is_exactly_the_pinned_characters(self):
        """正式碼刪的字元集合 == 上面逐一釘過的集合。

        往集合裡加字元(而沒有補一條上面那種測試)會在這裡停下來 —— 這道就是
        「每個刪掉的字元都要有紅燈」這條不變式的維護閘門。
        """
        from app.api import services as services_api

        stripped = {chr(code) for code in services_api._URL_CONTROL_STRIP}
        assert stripped == set(_WHATWG_STRIPPED)


# ── 1c. 發出去的字串 == 驗過的字串 ───────────────────────────────────────────


class TestLaunchUrlUsesTheValidatedString:
    """launch URL 必須由 ``_validate_launch_entry_url`` **回傳的**那個字串組成,
    不能回頭再讀一次 ``service.entry_url`` —— 驗一個、發另一個就是 TOCTOU 縫。

    釘的是兩個位置:``_validate_launch_entry_url`` 的 return,以及 launch 端點
    呼叫 ``build_launch_url`` 時傳進去的那個引數。任一處換成 ``service.entry_url``
    都要有測試變紅。

    ⚠ **這條的嚴重度說清楚**:目前查得到的差異全是「尾端空白/控制字元」這種
    形狀(``/anila``+空白、``/anila\\x0b``)。可觀察的後果是**路徑壞掉**(服務打
    不開),不是 origin 逃逸 —— 因為會逃逸的寫法在驗證階段就 400 了,根本走不到
    這裡。釘它的理由是這條不變式本身撐著整個設計,不是因為它現在能被打穿。
    (順帶一提:``/an\\tila`` 這種**夾在中間**的控制字元證明不了任何事,
    ``urlparse``/``urlunparse`` 自己就會把它抹掉,兩邊輸出一模一樣。)

    真實來源:治理中心表單裡 entry_url 尾巴多打一個空白。
    """

    @pytest.mark.parametrize(
        "trailing", [" ", "\x0b"], ids=["space", "vertical-tab"]
    )
    def test_launch_url_is_built_from_the_normalised_entry_url(
        self, client, db, trailing
    ):
        headers = _headers(client, db)
        svc = _make_service(
            db,
            name="尾端有空白",
            slug="trailing-ws",
            entry_url=f"/anila{trailing}",
            is_public=True,
        )
        body = client.post(
            f"/api/services/{svc.slug}/launch", json={}, headers=headers
        ).json()
        # 完全相等,不是 startswith —— 尾端那個字元有沒有被帶進來,只有等號看得出來。
        assert body["launch_url"] == f"/anila?launch_token={body['launch_token']}"
        assert urlparse(body["launch_url"]).path == "/anila"


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
    def test_gate_is_open_in_this_release(self):
        assert release_gate.ANILA_LM_RELEASED is True

    def test_present_in_user_facing_service_list(self, client, db):
        headers = _headers(client, db, username="erin")
        _make_anilalm(db)
        _make_service(
            db, name="ANILA", slug="anila", entry_url="/anila", is_public=True
        )
        rows = client.get("/api/services", headers=headers).json()
        names = [r["name"] for r in rows]
        assert "ANILA" in names
        assert "ANILA LM" in names

    def test_present_in_platform_links(self, client, db):
        headers = _headers(client, db, username="frank")
        _make_anilalm(db)
        rows = client.get("/api/platform-links", headers=headers).json()
        assert [r["name"] for r in rows if r["name"] == "ANILA LM"] == ["ANILA LM"]

    def test_admin_default_list_includes_anila_lm(self, client, db):
        headers = _headers(client, db, username="root", role="admin")
        _make_anilalm(db)
        rows = client.get("/api/services", headers=headers).json()
        assert [r["name"] for r in rows if r["name"] == "ANILA LM"] == ["ANILA LM"]

    def test_admin_can_still_see_and_manage_the_registration(self, client, db):
        """閘門開著時管理清單與 CRUD 仍正常。"""
        headers = _headers(client, db, username="root", role="admin")
        svc = _make_anilalm(db)

        listed = client.get(
            "/api/services?include_inactive=true", headers=headers
        ).json()
        assert [r["name"] for r in listed if r["name"] == "ANILA LM"] == ["ANILA LM"]
        links = client.get(
            "/api/platform-links?include_inactive=true", headers=headers
        ).json()
        lm_links = [r for r in links if r["name"] == "ANILA LM"]
        assert [r["name"] for r in lm_links] == ["ANILA LM"]
        assert lm_links[0]["release_gate_code"] == "anila_lm"

        edited = client.put(
            f"/api/services/{svc.id}",
            json={"description": "整備中"},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["description"] == "整備中"

        disabled = client.delete(f"/api/services/{svc.id}", headers=headers)
        assert disabled.status_code == 200, disabled.text

    def test_launch_allowed_while_gate_is_open(self, client, db):
        headers = _headers(client, db, username="gail")
        svc = _make_anilalm(db)
        assert svc.is_active is True
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["launch_url"].startswith("/anilalm?launch_token=")

    def test_launch_allowed_for_admin_too(self, client, db):
        headers = _headers(client, db, username="root", role="admin")
        svc = _make_anilalm(db)
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text

    def test_gate_can_be_closed_again(self, client, db, monkeypatch):
        monkeypatch.setattr(release_gate, "ANILA_LM_RELEASED", False)
        headers = _headers(client, db, username="hank")
        svc = _make_anilalm(db)
        rows = client.get("/api/services", headers=headers).json()
        assert [r for r in rows if r["name"] == "ANILA LM"] == []
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 503, resp.text


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
