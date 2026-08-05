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

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
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


# ══════════════════════════════════════════════════════════════════════════
# 2026-08-05:三個「訊號說謊」的回歸鎖
# ══════════════════════════════════════════════════════════════════════════
#
# 這張卡是單人維運者判斷「平台還好嗎」的唯一畫面,所以它往兩個方向說謊都是
# 致命的:把壞的畫成綠(管理者不知道要動手)、把好的畫成紅(卡片天天喊狼,
# 三天後沒人看,等於這個功能沒做)。下面三個測試各鎖一個當天量到的謊,而且
# 都驗行為 —— 只斷言常數長什麼樣的測試,把 production 改回去它照樣綠。


class _FakeResponse:
    """替身回應。探測只看 ``status_code``,所以這裡也只有它。"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    """替身 httpx 客戶端;``handler(url, headers)`` 扮演被探測的那個服務。"""

    def __init__(self, handler, **kwargs) -> None:
        self._handler = handler
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def get(self, url, headers=None):
        return self._handler(url, dict(headers or {}))


class _FakeRedis:
    """替身 redis:只實作探測用得到的 ``exists`` 與關閉。"""

    def __init__(self, keys, *, boom: bool = False, timeout: bool = False) -> None:
        self._keys = set(keys)
        self._boom = boom
        self._timeout = timeout
        self.asked: list[str] = []
        self.closed = False

    async def exists(self, key):
        self.asked.append(key)
        if self._timeout:
            # 與 ``asyncio.wait_for`` 逾時打到同一個 except 分支。
            raise asyncio.TimeoutError
        if self._boom:
            raise ConnectionError("redis 連不上")
        return 1 if key in self._keys else 0

    async def aclose(self) -> None:
        self.closed = True


class _RecordingResolves:
    """``_resolves`` 的替身,**會記下被問的是哪個名字**。

    只回傳常數的 lambda 有一個致命破綻:``_probe_queue_worker`` 把
    ``_resolves(name, 0)`` 改成 ``_resolves("redis", 0)`` 也照樣全綠 —— 而
    production 裡 ``redis`` 永遠解得出來,於是那一維被釘死在 True,**停掉的
    worker 會變綠燈**,正好是這一輪要殺的那個謊。所以問了誰必須被斷言。
    """

    def __init__(self, answer: bool) -> None:
        self._answer = answer
        self.hosts: list[str] = []

    def __call__(self, host, port) -> bool:
        self.hosts.append(host)
        return self._answer


def _install_fake_http(monkeypatch, handler) -> None:
    """換掉探測用的 httpx 客戶端,並讓 DNS 預檢一律通過。

    DNS 也要換:測試機沒有 compose 網路,``_resolves`` 會回 False,探測會在
    碰到替身之前就短路成 ``not_deployed`` —— 那樣測到的不是我們要測的東西。
    """
    monkeypatch.setattr(
        health_checker.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(handler, **kwargs),
    )
    monkeypatch.setattr(health_checker, "_resolves", lambda host, port: True)


def _is_anila_host_allowlist() -> set[str]:
    """從 ``infra/nginx/anila.conf`` 讀出 ``$is_anila_host`` 的字面 host。

    設定檔是真相,測試去讀它,而不是在測試裡再抄一份會靜默分歧的名單
    (同 ``test_nginx_upload_exposure`` 的姿態)。wildcard regex 那條不取 ——
    探測要帶的是一個確定的字面 host。
    """
    conf = (
        Path(__file__).resolve().parents[3] / "infra" / "nginx" / "anila.conf"
    ).read_text(encoding="utf-8")
    block = re.search(r"map \$host \$is_anila_host \{(.*?)\n\}", conf, re.DOTALL)
    assert block, "anila.conf 找不到 $is_anila_host map"
    return set(re.findall(r'"([^"]+)"\s+1;', block.group(1)))


def test_router_card_is_green_only_when_the_router_answered_about_itself(monkeypatch):
    """① router:404 不是綠燈。

    2026-08-05 量到:探測目標是 ``/ready``,router 根本沒有這條路徑,回 404;
    而舊的 ``<500 → healthy`` 把它畫成綠燈。也就是說總覽從來沒問過 router
    它好不好,卻一路說它好。

    替身 router 只有 ``/health`` 回 200,其餘一律 404:把探測目標改回任何
    router 沒有的路徑,第一段就紅;把綠燈門檻放寬回 ``<500``,第二段就紅。
    """
    asked: list[str] = []

    def only_health_answers(url, headers):
        path = urlsplit(url).path
        asked.append(path)
        return _FakeResponse(200 if path == "/health" else 404)

    _install_fake_http(monkeypatch, only_health_answers)
    result = asyncio.run(health_checker.probe_base_service("router"))

    assert (result.status, result.reason) == ("healthy", PROBE_OK)
    assert asked == ["/health"], f"探測沒去問 router 自己的健康端點:{asked}"

    # 同一支探測拿到 404 時**絕不可以**是綠燈 —— 那是「有東西在答話」,
    # 不是「這個服務回答了關於它自己健康的問題」。
    _install_fake_http(monkeypatch, lambda url, headers: _FakeResponse(404))
    not_found = asyncio.run(health_checker.probe_base_service("router"))

    assert not_found.status == "degraded"


def test_ingestion_worker_liveness_comes_from_the_queue_not_an_http_port(monkeypatch):
    """② ingestion-worker:別去敲一個它從來沒開過的 port。

    2026-08-05 量到:它是 arq queue worker,``infra/compose/platform.yml`` 沒
    給它 ports,8081 / 8080 / 8000 一律 connection refused。舊探測因此永遠紅,
    而 ``aggregate_health`` 取最差 → **整張總覽永遠紅**。

    真訊號是 worker 自己 psetex 進 redis 的 arq health-check key(TTL =
    ``WorkerSettings.health_check_interval`` + 1s)。四段:key 在 → 綠;
    key 過期 → 紅(所以**不是**一律綠,死掉的 worker 分得出來);redis 連不上
    → unknown;redis 逾時 → degraded。後兩段都不是綠 —— 「問不到」絕不能被
    當成「它很好」。把 ``_ARQ_HEALTH_CHECK_KEY`` 改成任何別的字,第一段就紅。
    """
    import redis.asyncio as aioredis

    _install_fake_http(
        monkeypatch,
        lambda url, headers: pytest.fail(f"佇列工作者不該被 HTTP 探測:{url}"),
    )
    assert "ingestion-worker" not in health_checker._HTTP_PROBE_TARGETS

    # 記名版的 _resolves:蓋掉 _install_fake_http 裝的常數 lambda,
    # 這樣「問的是哪個名字」才有斷言(見 _RecordingResolves)。
    resolver = _RecordingResolves(True)
    monkeypatch.setattr(health_checker, "_resolves", resolver)

    def _use(fake: _FakeRedis) -> None:
        monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: fake)

    # 刻意寫字面值,**不從 health_checker 讀那個常數**:讀常數的話,把常數改成
    # 別的字時替身也跟著改,測試照樣綠 —— 那種測試等於不存在。這個字面值是
    # 2026-08-05 從跑著的 worker 量到的(redis `KEYS *` 對得上),另一端由
    # services/ingestion-worker/tests/test_worker_liveness.py 從 arq 那側釘住。
    key = "arq:queue:health-check"

    alive = _FakeRedis({key})
    _use(alive)
    reporting = asyncio.run(health_checker.probe_base_service("ingestion-worker"))
    assert (reporting.status, reporting.reason) == ("healthy", PROBE_OK)
    assert alive.asked == [key], f"探測問錯了 key:{alive.asked}"
    assert alive.closed, "探測用的 redis 連線沒還回去"
    assert resolver.hosts == ["ingestion-worker"], (
        f"名稱解析問錯了對象:{resolver.hosts} —— 問任何一個「一定解得出來」的"
        f"名字(例如 redis)等於把這一維釘死在 True,停掉的 worker 會變綠燈"
    )

    dead = _FakeRedis(set())
    _use(dead)
    expired = asyncio.run(health_checker.probe_base_service("ingestion-worker"))
    assert expired.status == "unhealthy", "key 過期的 worker 必須紅,不能一律綠"

    _use(_FakeRedis(set(), boom=True))
    unknown = asyncio.run(health_checker.probe_base_service("ingestion-worker"))
    assert unknown.status == "unknown", "redis 壞掉不該把帳算到 worker 頭上"
    assert unknown.reason in PROBE_REASONS

    # redis 逾時同樣不是綠燈 —— 逾時的時候我們根本沒讀到 key。
    _use(_FakeRedis({key}, timeout=True))
    slow = asyncio.run(health_checker.probe_base_service("ingestion-worker"))
    assert (slow.status, slow.reason) == ("degraded", "timeout"), (
        "redis 逾時被畫成綠燈 = 問不到卻說它很好"
    )


def test_queue_worker_reads_dns_and_key_together(monkeypatch):
    """②b 名稱解析 ⊗ health key 的四格,一格都不能少。

    2026-08-05 驗收抓到的謊:容器一停,docker DNS 記錄就跟著消失(本機量到
    repo 有定義但沒起的 ``flux2-dev`` 直接 gaierror),於是「只用 DNS 判未部署」
    會把**掛掉的 worker** 畫成黃色的「此部署未啟用」—— 而那一格的說明是
    「不是故障」。管理者永遠不會看到紅燈。

    加上 key 這一維才分得出來:名字沒了但 key 還在 = 它剛剛還活著,現在容器
    不在了 → 紅,而且不必等 TTL。兩個訊號都不在,才是真的沒部署。
    """
    import redis.asyncio as aioredis

    key = "arq:queue:health-check"
    resolvers: list[_RecordingResolves] = []

    def _scenario(*, resolves: bool, key_present: bool):
        resolver = _RecordingResolves(resolves)
        resolvers.append(resolver)
        monkeypatch.setattr(health_checker, "_resolves", resolver)
        monkeypatch.setattr(
            aioredis,
            "from_url",
            lambda *a, **kw: _FakeRedis({key} if key_present else set()),
        )
        return asyncio.run(health_checker.probe_base_service("ingestion-worker"))

    # 正在跑而且在回報。
    assert _scenario(resolves=True, key_present=True).status == "healthy"

    # 容器在,但超過一個 TTL 沒回報 —— 卡死。
    assert _scenario(resolves=True, key_present=False).status == "unhealthy"

    # 名字沒了但 key 還在 = 容器停了。這一格就是驗收抓到的那個謊。
    stopped = _scenario(resolves=False, key_present=True)
    assert (stopped.status, stopped.reason) == ("unhealthy", "unreachable"), (
        "停掉的容器必須是紅的,不能畫成「此部署未啟用／不是故障」"
    )

    # 兩個都不在 = 這個部署沒有這個服務(或早就不在了),閉嘴比亂猜好。
    absent = _scenario(resolves=False, key_present=False)
    assert (absent.status, absent.reason) == ("unknown", "not_deployed")

    # 四格都必須是**問 ingestion-worker 這個名字**問出來的。改成任何一個
    # production 裡一定解得出來的名字(redis / csp / nginx…),這一維就被釘死
    # 在 True,停掉的 worker 會變綠燈,而上面四格照樣全過。
    for resolver in resolvers:
        assert resolver.hosts == ["ingestion-worker"], (
            f"名稱解析問錯了對象:{resolver.hosts}"
        )


def test_nginx_probe_speaks_a_host_the_allowlist_accepts(monkeypatch):
    """③ nginx:port-80 的 Host allowlist 是修補,不是故障。

    2026-08-05 量到:allowlist 合併後 ``Host: nginx`` 落在清單外 → nginx
    ``return 444`` 直接關連線 → httpx 丟 ``RemoteProtocolError``。那是
    ``ProtocolError`` 而**不是** ``NetworkError``,躲過既有的連線例外分支,
    掉進 generic except → 卡片紅,但 nginx 好好的。

    修法是探測改帶 allowlist 內的 Host,**不是**放寬 allowlist。替身 nginx
    照量到的真實行為演。把探測的 Host header 拿掉,第一段就紅。
    """
    allowlist = _is_anila_host_allowlist()
    # 這條同時鎖住「探測帶的 Host 必須真的在 nginx 那張 map 裡」:改成清單外
    # 的字、或 map 裡把它拿掉,這裡就紅,而不是等到線上才發現卡片變紅。
    probe_host = dict(health_checker._HTTP_PROBE_TARGETS["nginx"].headers).get("Host")
    assert probe_host in allowlist, (
        f"探測帶的 Host {probe_host!r} 不在 nginx 的 $is_anila_host 允許清單內"
    )

    def port_80_with_allowlist(url, headers):
        if headers.get("Host") not in allowlist:
            # nginx `return 444`:關連線,不送任何回應。
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return _FakeResponse(301)

    _install_fake_http(monkeypatch, port_80_with_allowlist)
    result = asyncio.run(health_checker.probe_base_service("nginx"))

    assert (result.status, result.reason) == ("healthy", PROBE_OK)

    # 反向:沒帶清單內的 Host 依然進不來。這個修法沒有動到 allowlist 的性質
    # —— 平台在攻擊者控制的 Host 之下仍然不可達。
    without_host = asyncio.run(
        health_checker._probe_http_service(
            "nginx",
            health_checker._HttpProbeTarget("http://nginx:80/", frozenset({301})),
        )
    )
    assert without_host.status != "healthy"
