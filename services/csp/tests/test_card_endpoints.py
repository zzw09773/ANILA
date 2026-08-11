"""Integration tests for ``/api/auth/card/{challenge,verify,complete-registration}``.

這是全 repo **唯一**行使 ``POST /api/auth/card/verify`` 的檔案,而卡登是這個平台
唯一的入口。所以這裡守的不是幾個 endpoint,是「八月底三千人進不進得來」。

2026-07-31 重寫的理由
=====================

原本的版本用 ``cht/app.py`` 的固定 mock 簽章 (eContent 永遠 ``b"TBS"``)。
2026-06-12 卡登從「只解析」改成「真驗證」後,``card_auth.py`` 多了 nonce 綁定
(eContent 必須等於本次 challenge 的隨機 nonce),而 mock 沒有私鑰、簽不出新
nonce —— **這個 mock 在架構上不可能通過驗證**。結果是 19 支測試紅了 10 支,
剩下 9 支全是負向守衛 (功能關閉回 404、壞 JWT 回 401),
其中 ``test_pending_user_does_not_get_session_cookies`` 更是綠得毫無內容:
它呼叫 verify 拿到 401,然後斷言 ``/me`` 是 401 —— 把整段 pending 註冊流程
從程式碼裡刪掉,那支測試照樣綠。

作法:合成一組**只在測試裡存在**的兩層 PKI (自簽 root → 卡片 leaf),把
``CARD_CA_BUNDLE_PATH`` 指過去,然後每次刷卡都拿當次 challenge 的 nonce 現簽
一份 CMS。這樣 nonce 綁定、CMS 簽章驗證、憑證鏈驗證、claim 抽取**全部照跑**,
沒有任何 dev 旁路旗標被打開 —— 唯一被替換的是信任錨。

代價 (明講):這個檔案不再證明「真實 CSPKI bundle 讀得起來、真實卡片憑證
驗得過」。那件事由 ``tests/test_card_auth.py`` 用真的 mock 卡材料 + 釘死的
``cspki_ca_bundle.pem`` 守著 (含自簽偽造工號的 CVE 回歸守衛),19 支全綠。
本檔則額外補了兩支端點層守衛 (``test_verify_rejects_signature_bound_to_a_previous_challenge``
與 ``test_verify_rejects_card_signed_by_untrusted_ca``),確保 endpoint 真的把
「這次的 nonce」與「釘死的信任錨」接進驗證,而不是只在單元層有。

Dev 手動刷卡 (``cht/`` mock 容器) 仍然要靠 ``CARD_DEV_SKIP_NONCE_BINDING=1``,
那條路徑不變,本檔不碰它。

契約
====
- ``settings.ANILA_AUTH_MODE="password"`` 時三個 endpoint 都回 404。
- ``GET /card/challenge`` 回 JWT + 明文 nonce。
- ``POST /card/verify``:驗章通過 → 建 User → 依核准狀態回 200 (含 cookie)
  或 202 (pending,不含 cookie)。
- 同一張卡刷兩次不 duplicate;email 撞既有帳號一律拒絕。
- pending → 選單位 → admin 核准 → 下次刷卡真的登入得了。
"""
from __future__ import annotations

import base64
import datetime
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

# ``cht/`` 是 repo 根目錄下的 dev mock(本機假的 HiPKI 元件),不是 csp 的
# 套件,所以用路徑掛進來。借的是它的**簽章器**,不是它的金鑰 —— 見 _build_cms。
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cht"))
from cms_sign import build_pkcs7  # noqa: E402

from app.config import settings
from app.middleware.cookies import (
    ACCESS_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
)
from app.models.user import User
from app.services import card_auth

from tests.conftest import make_user


# 合成身分。刻意不用真人的工號 / 姓名 / email —— repo 是 PUBLIC,而且這些憑證
# 是測試現場生出來的,跟中科院的任何一張真卡沒有關係。
# 工號要符合 ``card_auth._EMPLOYEE_ID_RE`` 的 6–9 位數字。
EMP_ID = "9000001"
DISPLAY_NAME = "測試員甲"
EMAIL = "card-a@example.invalid"
CARD_SN = "CSTEST0000000001"

OTHER_EMP_ID = "9000002"
OTHER_DISPLAY_NAME = "測試員乙"
OTHER_EMAIL = "card-b@example.invalid"


# ── 合成 PKI:root → 卡片 leaf,以及對應的 CMS SignedData 產生器 ────────────────


@dataclass(frozen=True)
class _Pki:
    root_key: rsa.RSAPrivateKey
    root_cert: x509.Certificate
    bundle_path: object  # pathlib.Path


@dataclass(frozen=True)
class _Card:
    """一張合成卡:私鑰 + 已由 root 簽發的憑證 + 憑證序號。"""

    key: rsa.RSAPrivateKey
    cert: x509.Certificate
    serial: int


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _issue_card(pki: _Pki, emp_id: str, cn: str, email: str, serial: int) -> _Card:
    """由合成 root 簽出一張卡片憑證,欄位擺放對齊真卡:
    ``subject.serialNumber`` = 員工編號、``subject.CN`` = 姓名、
    ``SAN.rfc822Name`` = email。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TEST-ONLY SYNTHETIC ORG"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, emp_id),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(pki.root_cert.subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False
        )
        .sign(pki.root_key, hashes.SHA256())
    )
    return _Card(key=key, cert=cert, serial=serial)


def _build_cms(card: _Card, econtent: bytes) -> str:
    """把 ``econtent`` 包成一份卡片私鑰簽出的 base64 CMS SignedData。

    **刻意直接借用 ``cht/cms_sign.py`` 的 ``build_pkcs7``** —— 那正是本機
    ``cht/`` mock 拿來簽章的同一段程式碼。這樣「一份合法卡片簽章長什麼樣」
    在測試與跑著的系統之間只有一個答案;以前兩邊各寫一份,測試全綠並不保證
    本機刷得進去(這個檔案 2026-07-31 重寫的理由就是這個)。

    金鑰素材**不**共用:測試用自己現生的拋棄式 PKI,不依賴這台機器上
    ``secrets/dev-card-ca/`` 有沒有東西,套件因此仍然 hermetic。
    """
    return base64.b64encode(build_pkcs7(econtent, card.key, card.cert)).decode()


@pytest.fixture(scope="session")
def card_pki(tmp_path_factory) -> _Pki:
    """自簽 root CA + 寫成 PEM bundle 檔。session scope —— RSA keygen 很貴。"""
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TEST-ONLY SYNTHETIC ORG"),
            x509.NameAttribute(NameOID.COMMON_NAME, "TEST-ONLY Synthetic Root CA"),
        ]
    )
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    bundle_path = tmp_path_factory.mktemp("card-pki") / "synthetic_ca_bundle.pem"
    bundle_path.write_bytes(root_cert.public_bytes(serialization.Encoding.PEM))
    return _Pki(root_key=root_key, root_cert=root_cert, bundle_path=bundle_path)


@pytest.fixture(scope="session")
def card_a(card_pki: _Pki) -> _Card:
    return _issue_card(card_pki, EMP_ID, DISPLAY_NAME, EMAIL, serial=1001)


@pytest.fixture(scope="session")
def card_b(card_pki: _Pki) -> _Card:
    return _issue_card(
        card_pki, OTHER_EMP_ID, OTHER_DISPLAY_NAME, OTHER_EMAIL, serial=1002
    )


@pytest.fixture(autouse=True)
def _trust_synthetic_pki(card_pki: _Pki, monkeypatch):
    """把釘死的信任錨換成合成 root,**只在本檔、只在單一 test 內**。

    ``_ca_anchor_cache`` 是 module global,清成 None 才會重讀 bundle;
    monkeypatch 記下原值,teardown 自動放回去,所以 ``test_card_auth.py``
    仍然是對著真的 ``cspki_ca_bundle.pem`` 跑。
    """
    monkeypatch.setenv("CARD_CA_BUNDLE_PATH", str(card_pki.bundle_path))
    monkeypatch.setattr(card_auth, "_ca_anchor_cache", None)


@pytest.fixture
def card_login_enabled(monkeypatch):
    """開功能旗標,並把測試員甲設為 initial owner → 刷卡直接登入。"""
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "mixed")
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", EMP_ID)


@pytest.fixture
def card_login_disabled(monkeypatch):
    """關閉功能旗標 —— 負向測試必須自己釘 precondition,不可依賴 ambient 預設。"""
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "password")


@pytest.fixture
def card_login_pending_default(monkeypatch):
    """同上但 ``CARD_INITIAL_OWNERS`` 為空 —— 所有人第一次刷卡都進 pending。"""
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "mixed")
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", "")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _do_card_verify(client: TestClient, card: _Card, *, card_serial: str | None = None):
    """完整跑一次 challenge → 用當次 nonce 現簽 → verify,回 ``Response``。"""
    ch = client.get("/api/auth/card/challenge").json()
    payload = {
        "challenge_token": ch["challenge_token"],
        "signature": _build_cms(card, ch["nonce"].encode("utf-8")),
    }
    if card_serial is not None:
        payload["card_serial"] = card_serial
    # 已經有 session 時再刷一次卡(重新登入),CSRF middleware 會要求 echo
    # X-CSRF-Token —— 瀏覽器端的前端本來就會這樣送。不補這個 header 會拿到
    # 403,那是 CSRF 防線正常運作,不是卡登壞掉。
    headers = {}
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.post("/api/auth/card/verify", json=payload, headers=headers)


def _make_department(db, name: str = "資通所人工智慧組") -> int:
    from app.models.department import Department

    dept = Department(name=name, is_active=True)
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept.id


# ── disabled-by-default guard ──────────────────────────────────────────────────


def test_challenge_returns_404_when_card_login_disabled(
    client: TestClient, card_login_disabled
):
    resp = client.get("/api/auth/card/challenge")
    assert resp.status_code == 404


def test_verify_returns_404_when_card_login_disabled(
    client: TestClient, card_login_disabled
):
    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "x", "signature": "y"},
    )
    assert resp.status_code == 404


# ── challenge ─────────────────────────────────────────────────────────────────


def test_challenge_returns_jwt_and_plaintext_nonce(
    client: TestClient, card_login_enabled
):
    resp = client.get("/api/auth/card/challenge")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body["challenge_token"], str) and body["challenge_token"]
    assert isinstance(body["nonce"], str) and len(body["nonce"]) >= 16
    assert body["expires_in"] == 120


def test_challenge_nonce_differs_every_time(client: TestClient, card_login_enabled):
    """nonce 是隨機的 —— 固定 nonce 會讓反 replay 整個失效。"""
    seen = {client.get("/api/auth/card/challenge").json()["nonce"] for _ in range(5)}
    assert len(seen) == 5


# ── 行為 ①:帳號建立 (auto-provision) ─────────────────────────────────────────


def test_verify_creates_user_and_sets_session_cookies(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    resp = _do_card_verify(client, card_a, card_serial=CARD_SN)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["csrf_token"]

    cookie_names = {c.name for c in client.cookies.jar}
    assert ACCESS_COOKIE_NAME in cookie_names
    assert REFRESH_COOKIE_NAME in cookie_names

    user = db.query(User).filter(User.username == EMP_ID).first()
    assert user is not None, "驗章通過的卡片應該自動建帳號"
    assert user.email == EMAIL
    assert user.local_password_disabled is True
    assert user.is_approved is True
    assert user.is_active is True


def test_me_reachable_with_card_session_cookie(
    client: TestClient, card_a: _Card, card_login_enabled
):
    assert _do_card_verify(client, card_a).status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == EMP_ID


def test_owner_in_initial_owners_logs_in_directly(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    """``CARD_INITIAL_OWNERS`` 內的工號 → role=owner 且直接 approved。"""
    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 200
    assert "access_token" in resp.json()

    user = db.query(User).filter(User.username == EMP_ID).first()
    assert user.role == "owner"
    assert user.is_approved is True


def test_second_login_reuses_existing_user(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    """同一張卡刷兩次不會 duplicate user row。"""
    for _ in range(2):
        assert _do_card_verify(client, card_a).status_code == 200

    assert db.query(User).filter(User.username == EMP_ID).count() == 1


# ── 行為 ②:email 撞號拒絕 ────────────────────────────────────────────────────


def test_email_collision_with_other_account_is_rejected(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    """既有帳號 (非卡片帳號) 已佔用同 email → 卡片登入被拒,不自動接管。

    policy 對齊 OIDC ``_provision_external_user``:要 admin 手動處理。
    """
    other = make_user(db, username="other-user")
    other.email = EMAIL
    db.commit()

    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 400, resp.text
    assert "已綁定其他帳號" in resp.json()["detail"]
    # 而且沒有偷偷建出第二個帳號
    assert db.query(User).filter(User.username == EMP_ID).first() is None


def test_email_collision_does_not_hijack_existing_account(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    """被撞的那個帳號不該被改動,也不該發出 session。"""
    other = make_user(db, username="other-user")
    other.email = EMAIL
    db.commit()
    other_id = other.id

    _do_card_verify(client, card_a)

    db.expire_all()
    still = db.query(User).filter(User.id == other_id).first()
    assert still.username == "other-user"
    assert still.local_password_disabled is not True
    assert ACCESS_COOKIE_NAME not in {c.name for c in client.cookies.jar}


# ── 行為 ③:pending registration ──────────────────────────────────────────────


def test_non_owner_first_swipe_returns_pending_registration(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    """OWNERS 為空 → 一般申請者,回 202 pending_registration + token。"""
    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending_registration"
    assert body["employee_id"] == EMP_ID
    assert body["display_name"] == DISPLAY_NAME
    assert body["email"] == EMAIL
    assert body["registration_token"]
    assert body["expires_in"] == 900

    # User row 該被建起來，但 role=user / is_approved=False / department_id=None
    user = db.query(User).filter(User.username == EMP_ID).first()
    assert user is not None
    assert user.role == "user"
    assert user.is_approved is False
    assert user.department_id is None


def test_pending_user_does_not_get_session_cookies(
    client: TestClient, card_a: _Card, card_login_pending_default
):
    """Pending 回應不該種 cookie — ``/me`` 仍然 401。

    ⚠ 這支測試 2026-07-31 以前是**空的綠燈**:verify 因為 nonce 綁不上而回 401,
    pending 分支根本沒被執行,然後斷言 ``/me`` 是 401 —— 當然是。下面第一行的
    ``== 202`` 就是防止它再退化回去的鎖:沒真的走進 pending 分支就不算數。
    """
    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "pending_registration"
    assert not {ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME} & {
        c.name for c in client.cookies.jar
    }

    me = client.get("/api/auth/me")
    assert me.status_code == 401


# ── 行為 ④:選單位完成註冊 ───────────────────────────────────────────────────


def test_complete_registration_with_valid_department(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    """Pending → 帶 token + 有效 department_id → 200,user.department_id 更新。"""
    dept_id = _make_department(db, "資通所人工智慧組")
    pending = _do_card_verify(client, card_a).json()

    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert "等待管理員核准" in body["message"]

    db.expire_all()
    user = db.query(User).filter(User.username == EMP_ID).first()
    assert user.department_id == dept_id
    assert user.is_approved is False  # 仍然要等 admin 核准


def test_complete_registration_with_invalid_token(
    client: TestClient, db, card_login_pending_default
):
    _make_department(db)
    resp = client.post(
        "/api/auth/card/complete-registration",
        json={"registration_token": "not.a.valid.jwt", "department_id": 1},
    )
    assert resp.status_code == 401


def test_complete_registration_with_invalid_department(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    pending = _do_card_verify(client, card_a).json()
    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": 9999,  # 不存在
        },
    )
    assert resp.status_code == 400

    # 而且不該把 department_id 寫成那個不存在的值
    db.expire_all()
    user = db.query(User).filter(User.username == EMP_ID).first()
    assert user.department_id is None


def test_complete_registration_rejects_inactive_department(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    """已裁撤的單位不能被選 —— 下拉不列它,直接送 id 也要擋。"""
    from app.models.department import Department

    dead = Department(name="已裁撤組", is_active=False)
    db.add(dead)
    db.commit()
    db.refresh(dead)

    pending = _do_card_verify(client, card_a).json()
    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dead.id,
        },
    )
    assert resp.status_code == 400


def test_second_swipe_after_registration_returns_pending_approval(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    """已填單位 + 仍 is_approved=False → 第二次刷卡回 pending_approval (無 token)。"""
    dept_id = _make_department(db)

    pending = _do_card_verify(client, card_a).json()
    client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )

    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["registration_token"] is None  # 不再發 token
    assert "等待管理員核准" in body["message"]


# ── 行為 ⑤:admin 核准後才真的登入得了 ───────────────────────────────────────


def test_admin_approval_unblocks_login(
    client: TestClient, db, card_a: _Card, card_login_pending_default
):
    """Admin 把 is_approved 翻為 True 後,下次刷卡應該真的拿到 cookie session。"""
    dept_id = _make_department(db)
    pending = _do_card_verify(client, card_a).json()
    client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )

    # 核准前:進不來
    assert _do_card_verify(client, card_a).status_code == 202

    # 模擬 admin 核准
    db.expire_all()
    user = db.query(User).filter(User.username == EMP_ID).first()
    user.is_approved = True
    db.commit()

    resp = _do_card_verify(client, card_a)
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()
    assert ACCESS_COOKIE_NAME in {c.name for c in client.cookies.jar}

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == EMP_ID


# ── 錯誤與攻擊面 ──────────────────────────────────────────────────────────────


def test_verify_rejects_malformed_challenge_token(
    client: TestClient, card_a: _Card, card_login_enabled
):
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": "not.a.valid.jwt",
            "signature": _build_cms(card_a, b"whatever"),
        },
    )
    assert resp.status_code == 400


def test_verify_rejects_invalid_signature_with_401(
    client: TestClient, card_login_enabled
):
    ch = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": ch["challenge_token"], "signature": "not-base64!!!"},
    )
    assert resp.status_code == 401


def test_verify_rejects_signature_bound_to_a_previous_challenge(
    client: TestClient, db, card_a: _Card, card_login_enabled
):
    """反 replay:拿舊 challenge 的簽章配新 challenge token → 401。

    這支守的是 **endpoint 有沒有把「本次的」nonce 接進驗證**。單元層的
    ``test_card_auth.py::test_wrong_nonce_rejected`` 只證明函式會比對,證明不了
    ``card_auth_service`` 傳下去的是這次 challenge 的 nonce。
    """
    old = client.get("/api/auth/card/challenge").json()
    stale_signature = _build_cms(card_a, old["nonce"].encode("utf-8"))

    fresh = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": fresh["challenge_token"],
            "signature": stale_signature,
        },
    )
    assert resp.status_code == 401, resp.text
    assert db.query(User).filter(User.username == EMP_ID).first() is None


def test_verify_rejects_card_signed_by_untrusted_ca(
    client: TestClient, db, card_pki: _Pki, card_login_enabled
):
    """自簽一張填了 owner 工號的卡 → 鏈連不到信任錨 → 401,不得建帳號。

    ``test_card_auth.py`` 對真 bundle 守過同一件事;這裡守的是 endpoint 沒有
    在 service 層繞過鏈驗證。
    """
    rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(NameOID.COMMON_NAME, "駭客"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, EMP_ID),
        ]
    )
    rogue_cert = (
        x509.CertificateBuilder()
        .subject_name(rogue_name)
        .issuer_name(rogue_name)
        .public_key(rogue_key.public_key())
        .serial_number(7777)
        .not_valid_before(_now() - datetime.timedelta(days=1))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(EMAIL)]), critical=False
        )
        .sign(rogue_key, hashes.SHA256())
    )
    rogue = _Card(key=rogue_key, cert=rogue_cert, serial=7777)

    resp = _do_card_verify(client, rogue)
    assert resp.status_code == 401, resp.text
    assert db.query(User).filter(User.username == EMP_ID).first() is None


def test_verify_rejects_card_from_a_different_person(
    client: TestClient, db, card_b: _Card, card_login_pending_default
):
    """另一張合法卡建的是另一個帳號,不會混進第一張卡的帳號。"""
    resp = _do_card_verify(client, card_b)
    assert resp.status_code == 202, resp.text
    assert resp.json()["employee_id"] == OTHER_EMP_ID
    assert db.query(User).filter(User.username == EMP_ID).first() is None
    assert db.query(User).filter(User.username == OTHER_EMP_ID).first() is not None


# ── departments 下拉 ─────────────────────────────────────────────────────────


def test_departments_endpoint_lists_only_active(
    client: TestClient, db, card_login_pending_default
):
    """``/api/auth/card/registration/departments`` 應該 public + 只列 active。"""
    from app.models.department import Department

    _make_department(db, "資通所人工智慧組")
    inactive = Department(name="已裁撤組", is_active=False)
    db.add(inactive)
    db.commit()

    resp = client.get("/api/auth/card/registration/departments")
    assert resp.status_code == 200
    rows = resp.json()
    names = {r["name"] for r in rows}
    assert "資通所人工智慧組" in names
    assert "已裁撤組" not in names
    # 僅暴露 id + name 兩個欄位
    assert all(set(r.keys()) == {"id", "name"} for r in rows)


def test_departments_endpoint_404_when_card_login_disabled(
    client: TestClient, card_login_disabled
):
    """同 challenge / verify,feature off 時 endpoint 假裝不存在。"""
    resp = client.get("/api/auth/card/registration/departments")
    assert resp.status_code == 404
