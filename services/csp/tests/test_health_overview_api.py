"""服務健康總覽端點的驗收(W3-3⑦)。

對應驗收條目
------------
① 非 admin → 403;admin → 200 且含各服務狀態與 ``checked_at``
② 某個服務探測失敗 → 該服務紅/黃,而**整個端點仍回 200**
③ 回應不含連線字串 / 密碼 / 內部 IP —— 逐欄白名單(安全紅線)
④ 探測目標無法由呼叫端指定

為什麼全部用 monkeypatch 探測
-----------------------------
專案鐵則:不動 running ``anila-platform-*`` 容器。真的去打 dev stack 的
router/redis 會① 依賴 host 上的偶然狀態② 讓測試在別人機器上紅。所以這裡
把探測函式換掉,驗的是**端點的彙總、降級與洩漏行為**,那才是本包的契約面。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import re

import pytest
from sqlalchemy.orm import Session

from app.api.admin import health_overview as overview_module
from app.services import health_checker
from app.services.auth_service import create_tokens
from app.services.health_checker import (
    BASE_SERVICE_SPECS,
    PROBE_OK,
    PROBE_REASONS,
    FIVE_STATE_HEALTH,
    ServiceProbeResult,
)
from tests.conftest import make_agent, make_model, make_user

OVERVIEW_URL = "/api/admin/health/overview"

#: 回應允許的欄位。任何新增欄位都要先過這條 —— 這是驗收③的紅線。
TOP_LEVEL_KEYS = {"overall", "checked_at", "services", "models", "agents"}
SERVICE_KEYS = {
    "name",
    "label",
    "kind",
    "status",
    "reason",
    "latency_ms",
    "checked_at",
}
COUNT_KEYS = {"total", "unknown", "healthy", "degraded", "unhealthy", "disabled"}


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


@pytest.fixture
def all_healthy(monkeypatch):
    """所有基礎服務都健康 —— 不碰網路、不碰 dev stack。"""

    async def fake_probe(name: str, *, db=None):
        assert name in {spec.name for spec in BASE_SERVICE_SPECS}
        return ServiceProbeResult("healthy", PROBE_OK, 3)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)
    return fake_probe


# ── ① 授權 + 基本形狀 ──────────────────────────────────────────────────────


def test_overview_requires_authentication(client):
    assert client.get(OVERVIEW_URL).status_code == 401


def test_overview_rejects_non_admin(client, db: Session, all_healthy):
    user = make_user(db, username="health-plain", role="user")

    resp = client.get(OVERVIEW_URL, headers=_bearer(user))

    assert resp.status_code == 403, resp.text


def test_overview_rejects_developer(client, db: Session, all_healthy):
    dev = make_user(db, username="health-dev", role="developer")

    assert client.get(OVERVIEW_URL, headers=_bearer(dev)).status_code == 403


def test_overview_admin_gets_every_service_with_checked_at(
    client, db: Session, all_healthy
):
    admin = make_user(db, username="health-admin", role="admin")

    resp = client.get(OVERVIEW_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [svc["name"] for svc in body["services"]]
    # 稽核 D1 點名的七個基礎服務 + csp 自己,一個都不能少。
    for expected in (
        "csp",
        "csp-db",
        "redis",
        "nginx",
        "router",
        "ingestion-worker",
        "anila-studio",
        "pptx-renderer",
    ):
        assert expected in names, f"總覽缺少 {expected}"
    assert body["overall"] == "healthy"
    assert body["checked_at"].endswith("Z")
    for svc in body["services"]:
        assert svc["status"] in FIVE_STATE_HEALTH
        assert svc["reason"] in PROBE_REASONS
        assert svc["checked_at"].endswith("Z")


def test_overview_owner_also_allowed(client, db: Session, all_healthy):
    owner = make_user(db, username="health-owner", role="owner")

    assert client.get(OVERVIEW_URL, headers=_bearer(owner)).status_code == 200


def test_overview_counts_registry_five_state(client, db: Session, all_healthy):
    admin = make_user(db, username="health-counts", role="admin")
    model = make_model(db, name="counts-model")
    model.health_status = "degraded"
    agent = make_agent(db, admin, name="counts-agent", approval_status="approved")
    agent.health_status = "unhealthy"
    db.commit()

    body = client.get(OVERVIEW_URL, headers=_bearer(admin)).json()

    assert body["models"]["total"] == 1
    assert body["models"]["degraded"] == 1
    assert body["agents"]["total"] == 1
    assert body["agents"]["unhealthy"] == 1


def test_overview_marks_unapproved_agent_disabled(client, db: Session, all_healthy):
    admin = make_user(db, username="health-pending-agent", role="admin")
    agent = make_agent(db, admin, name="pending-agent", approval_status="pending")
    agent.health_status = "healthy"
    db.commit()

    body = client.get(OVERVIEW_URL, headers=_bearer(admin)).json()

    # 未核准的 agent 不該被算成 healthy —— 沿用 normalize_health_status 的姿態。
    assert body["agents"]["disabled"] == 1
    assert body["agents"]["healthy"] == 0


# ── ② 單一服務掛掉不讓總覽掛掉 ────────────────────────────────────────────


def test_single_service_failure_keeps_endpoint_200(client, db: Session, monkeypatch):
    admin = make_user(db, username="health-partial", role="admin")

    async def fake_probe(name: str, *, db=None):
        if name == "router":
            return ServiceProbeResult("unhealthy", "unreachable", 4001)
        if name == "anila-studio":
            return ServiceProbeResult("degraded", "timeout", 4000)
        return ServiceProbeResult("healthy", PROBE_OK, 2)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)

    resp = client.get(OVERVIEW_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {svc["name"]: svc for svc in body["services"]}
    assert by_name["router"]["status"] == "unhealthy"
    assert by_name["anila-studio"]["status"] == "degraded"
    assert by_name["csp"]["status"] == "healthy"
    # overall 取最差 → 紅。
    assert body["overall"] == "unhealthy"


def test_probe_raising_does_not_500_the_overview(client, db: Session, monkeypatch):
    """probe 內部漏例外也只降級那一列。"""
    admin = make_user(db, username="health-boom", role="admin")

    async def fake_probe(name: str, *, db=None):
        if name == "pptx-renderer":
            raise RuntimeError("psycopg2.OperationalError: postgresql://csp_app:hunter2@csp-db:5432/csp")
        return ServiceProbeResult("healthy", PROBE_OK, 1)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)

    resp = client.get(OVERVIEW_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {svc["name"]: svc for svc in body["services"]}
    assert by_name["pptx-renderer"]["status"] == "unknown"
    assert by_name["pptx-renderer"]["reason"] in PROBE_REASONS
    # 例外訊息裡的連線字串絕不能被搬進回應。
    assert "hunter2" not in resp.text
    assert "postgresql://" not in resp.text


def test_degraded_service_yields_yellow_overall(client, db: Session, monkeypatch):
    admin = make_user(db, username="health-yellow", role="admin")

    async def fake_probe(name: str, *, db=None):
        if name == "redis":
            return ServiceProbeResult("degraded", "timeout", 4000)
        return ServiceProbeResult("healthy", PROBE_OK, 1)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)

    body = client.get(OVERVIEW_URL, headers=_bearer(admin)).json()

    assert body["overall"] == "degraded"


def test_db_probe_failure_nulls_registry_counts(client, db: Session, monkeypatch):
    """DB 掛了就回 null,不回 0 —— 0 是謊,null 是「這輪沒讀到」。"""
    admin = make_user(db, username="health-nodb", role="admin")

    async def fake_probe(name: str, *, db=None):
        if name == "csp-db":
            return ServiceProbeResult("unhealthy", "unreachable", 12)
        return ServiceProbeResult("healthy", PROBE_OK, 1)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)

    resp = client.get(OVERVIEW_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] is None
    assert body["agents"] is None
    assert body["overall"] == "unhealthy"


# ── ③ 逐欄白名單:不洩連線字串 / 密碼 / 內部 IP(安全紅線) ────────────────


def test_response_fields_are_exactly_the_whitelist(client, db: Session, all_healthy):
    admin = make_user(db, username="health-whitelist", role="admin")

    body = client.get(OVERVIEW_URL, headers=_bearer(admin)).json()

    assert set(body.keys()) == TOP_LEVEL_KEYS
    for svc in body["services"]:
        assert set(svc.keys()) == SERVICE_KEYS, svc
    for block in ("models", "agents"):
        assert set(body[block].keys()) == COUNT_KEYS


def test_response_leaks_no_connection_string_password_or_internal_ip(
    client, db: Session, all_healthy, monkeypatch
):
    """這條是安全紅線:逐一比對真實的部署祕密與內部位址。"""
    from app.config import settings

    admin = make_user(db, username="health-noleak", role="admin")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    raw = client.get(OVERVIEW_URL, headers=_bearer(admin)).text

    # (a) 真實設定值:DB 連線字串、SECRET_KEY、admin 密碼。
    for secret in (
        settings.DATABASE_URL,
        settings.SECRET_KEY,
        settings.ADMIN_PASSWORD,
    ):
        if secret:
            assert secret not in raw, "回應洩漏了部署祕密"
    # (b) 連線字串 / URL 的任何形狀。
    for scheme in ("postgresql://", "postgres://", "redis://", "http://", "https://"):
        assert scheme not in raw, f"回應含 {scheme} —— 探測目標不得外洩"
    # (c) port 與內部 IP。
    for port in (":5432", ":6379", ":8000", ":8081", ":8100", ":9000", ":7100"):
        assert port not in raw, f"回應含 {port} —— 部署細節不得外洩"
    assert not re.search(r"\b(?:10|127|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", raw), (
        "回應含內部 IP"
    )
    # (d) 一般性的祕密字樣。
    for token in ("password", "passwd", "secret", "token", "PASSWORD", "SECRET"):
        assert token not in raw, f"回應含 {token}"


def test_reason_is_bounded_vocabulary_not_free_text(client, db: Session, monkeypatch):
    """reason 必須是有限字彙 —— 自由文字就是把後端例外訊息送到瀏覽器。"""
    admin = make_user(db, username="health-reason", role="admin")

    async def fake_probe(name: str, *, db=None):
        return ServiceProbeResult("unhealthy", "unreachable", 7)

    monkeypatch.setattr(overview_module, "probe_base_service", fake_probe)

    body = client.get(OVERVIEW_URL, headers=_bearer(admin)).json()

    for svc in body["services"]:
        assert svc["reason"] in PROBE_REASONS


# ── ④ 探測目標不可由呼叫端指定 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "?host=10.53.100.99",
        "?port=22",
        "?host=169.254.169.254&port=80",
        "?url=http://10.0.0.1:8080/",
        "?target=csp-db",
        "?service=redis",
        "?anything=1",
    ],
)
def test_caller_cannot_supply_a_probe_target(client, db: Session, all_healthy, query):
    admin = make_user(db, username=f"health-q-{abs(hash(query)) % 10000}", role="admin")

    resp = client.get(OVERVIEW_URL + query, headers=_bearer(admin))

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "不可由呼叫端指定" in detail


def test_probe_target_list_is_hardcoded_and_closed(client, db: Session, monkeypatch):
    """真正被探測的目標集合 == 寫死的清單,無論呼叫端送什麼。"""
    admin = make_user(db, username="health-closed", role="admin")
    probed: list[str] = []

    async def recording_probe(name: str, *, db=None):
        probed.append(name)
        return ServiceProbeResult("healthy", PROBE_OK, 1)

    monkeypatch.setattr(overview_module, "probe_base_service", recording_probe)

    resp = client.get(OVERVIEW_URL, headers=_bearer(admin))

    assert resp.status_code == 200
    assert probed == [spec.name for spec in BASE_SERVICE_SPECS]


def test_endpoint_declares_no_query_parameters():
    """契約層:路由簽章不得出現 host/port/url 之類的參數。"""
    import inspect

    params = inspect.signature(overview_module.health_overview).parameters
    for forbidden in ("host", "port", "url", "target", "service", "endpoint"):
        assert forbidden not in params, f"端點不得宣告 {forbidden} 參數"


def test_probe_base_service_rejects_unknown_target():
    """探測函式本身也不是通用連線工具。"""
    import asyncio

    with pytest.raises(KeyError):
        asyncio.run(health_checker.probe_base_service("evil-host"))
    with pytest.raises(KeyError):
        asyncio.run(health_checker.probe_base_service("10.53.100.99:22"))


# ── 探測函式的單元行為(不打網路) ──────────────────────────────────────────


def test_aggregate_health_takes_the_worst():
    assert health_checker.aggregate_health([]) == "unknown"
    assert health_checker.aggregate_health(["healthy", "healthy"]) == "healthy"
    assert health_checker.aggregate_health(["healthy", "unknown"]) == "unknown"
    assert health_checker.aggregate_health(["healthy", "degraded"]) == "degraded"
    assert (
        health_checker.aggregate_health(["degraded", "unhealthy", "healthy"])
        == "unhealthy"
    )
    # disabled 是刻意停用,不該把總覽拉黃。
    assert health_checker.aggregate_health(["healthy", "disabled"]) == "healthy"


def test_summarize_five_state_normalizes_legacy_values():
    counts = health_checker.summarize_five_state(
        [("online", True), ("offline", True), ("connecting", True), (None, False)]
    )
    assert counts == {
        "total": 4,
        "unknown": 0,
        "healthy": 1,
        "degraded": 1,
        "unhealthy": 1,
        "disabled": 1,
    }


def test_database_probe_reports_unhealthy_without_leaking_the_dsn(caplog):
    class _BoomSession:
        def execute(self, _stmt):
            raise RuntimeError(
                "could not connect to server: postgresql://csp_app:hunter2@csp-db:5432/csp"
            )

        def rollback(self):
            return None

    result = health_checker.probe_database(_BoomSession())

    assert result.status == "unhealthy"
    assert result.reason == "unreachable"
    # ServiceProbeResult 沒有承載訊息的欄位 —— 結構上就洩不出去。
    assert not hasattr(result, "message")
    assert set(result.__slots__) == {"status", "reason", "latency_ms"}


def test_unresolvable_host_is_unknown_not_unhealthy(monkeypatch):
    """名稱解不出來 = 這個部署沒起這個服務,不是「掛了」。

    三條部署分支 + compose profiles 下,把「沒部署」畫成紅點會讓卡片天天
    喊狼,管理者三天後就不看了 —— 那等於功能沒做。
    """
    import asyncio

    monkeypatch.setattr(health_checker, "_resolves", lambda host, port: False)

    result = asyncio.run(health_checker.probe_base_service("pptx-renderer"))

    assert result.status == "unknown"
    assert result.reason == "not_deployed"


def test_outbound_probes_do_not_receive_the_request_db(client, db: Session, monkeypatch):
    """Re-graft invariant: no request session across outbound probes.

    Local probes (csp / csp-db) may see db; HTTP/redis must be called with
    db=None so the overview cannot pin a pooled connection across awaits.
    """
    admin = make_user(db, username="health-no-db-across", role="admin")
    seen: dict[str, object] = {}

    async def recording_probe(name: str, *, db=None):
        seen[name] = db
        return ServiceProbeResult("healthy", PROBE_OK, 1)

    monkeypatch.setattr(overview_module, "probe_base_service", recording_probe)

    assert client.get(OVERVIEW_URL, headers=_bearer(admin)).status_code == 200

    assert seen["csp-db"] is not None  # DB probe uses the request session
    for name in ("redis", "nginx", "router", "ingestion-worker", "anila-studio", "pptx-renderer"):
        assert name in seen
        assert seen[name] is None, f"{name} must not receive the request db"
