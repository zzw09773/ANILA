"""Regression: the CSRF exemption must be decided on the *routed* path.

The defect these tests pin is a disagreement between two layers of the
same request:

- ``CsrfMiddleware`` used to read ``request.url.path``. On starlette
  < 1.0.1 (CVE-2026-48710) ``request.url`` is built by concatenating the
  raw ``Host`` header with the path, so a request sent with
  ``Host: attacker.example/api/auth/login`` yields
  ``url.path == "/api/auth/login/api/users/1/approve"`` — an exempt
  prefix.
- The router matches on ``scope["path"]``, which is unchanged, so the
  request still reached ``approve_user``.

Result: a state-changing endpoint executed with the victim's session
cookie and no ``X-CSRF-Token`` at all. Because the two layers read
different strings, a test that only exercises ``_should_skip`` would not
have caught it — every end-to-end case below goes through the real
application so the middleware verdict and the routing decision are
observed on the same request.

These tests are behavioural: none of them inspects the source text of
``app.middleware.csrf``, so reverting the fix must turn them red.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.datastructures import URL
from starlette.requests import HTTPConnection, Request
from starlette.routing import Host, Mount, Route

from app.middleware.cookies import ACCESS_COOKIE_NAME, CSRF_COOKIE_NAME
from app.middleware.csrf import _matches_exempt_prefix, _should_skip

from tests.conftest import make_user

# A path prefix that is CSRF-exempt, used by the library-regression tests
# below to make a substituted ``request.url`` disagree with the routed path.
EXEMPT_PREFIX = "/api/auth/login"

# Host headers carrying a path segment. Each one moves a different entry
# of ``_EXEMPT_PREFIXES`` in front of the real path.
POLLUTED_HOSTS = [
    "attacker.example/api/auth/login",
    "attacker.example/api/auth/register",
    "attacker.example/api/auth/providers",
    "attacker.example/api/auth/oidc/",
    "attacker.example/health",
    "attacker.example/docs",
    "attacker.example/openapi.json",
    "attacker.example/static/",
]

# The state-changing endpoint used as the target. ``POST
# /api/users/{id}/approve`` flips ``is_approved`` and writes an audit
# row, so the assertion can be "did the state actually change", not just
# "what status code came back".
PROTECTED_PATH = "/api/users/{user_id}/approve"


def _login(client: TestClient, username: str) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert resp.status_code == 200, resp.text


def _make_actor_and_victim(db):
    actor = make_user(db, username="csrf-admin", role="admin")
    victim = make_user(db, username="csrf-victim", is_approved=False)
    return actor, victim


@pytest.mark.parametrize("host", POLLUTED_HOSTS)
def test_polluted_host_cannot_reach_state_change(client: TestClient, db, host):
    """A Host header carrying an exempt prefix must not skip the check."""
    actor, victim = _make_actor_and_victim(db)
    _login(client, actor.username)

    resp = client.post(
        PROTECTED_PATH.format(user_id=victim.id),
        headers={"Host": host},  # no X-CSRF-Token — this is the forgery
    )

    assert resp.status_code == 403, resp.text
    assert "CSRF" in resp.json()["detail"]

    db.refresh(victim)
    assert victim.is_approved is False, (
        f"Host={host!r} walked a state change past CSRF"
    )


def test_clean_host_without_token_is_still_blocked(client: TestClient, db):
    """Control: the target endpoint really is CSRF-protected."""
    actor, victim = _make_actor_and_victim(db)
    _login(client, actor.username)

    resp = client.post(PROTECTED_PATH.format(user_id=victim.id))

    assert resp.status_code == 403, resp.text
    db.refresh(victim)
    assert victim.is_approved is False


def test_clean_host_with_token_still_works(client: TestClient, db):
    """Control: the fix must not break the legitimate SPA flow."""
    actor, victim = _make_actor_and_victim(db)
    _login(client, actor.username)

    resp = client.post(
        PROTECTED_PATH.format(user_id=victim.id),
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )

    assert resp.status_code == 200, resp.text
    db.refresh(victim)
    assert victim.is_approved is True


def test_login_stays_exempt_while_a_session_cookie_is_present(
    client: TestClient, db
):
    """The exemption still has to exempt what it is there for.

    Re-authenticating while a stale ``anila_access_token`` cookie is
    still in the jar is the exact case ``/api/auth/login`` is exempt
    for — the SPA has no CSRF token it can trust yet.
    """
    make_user(db, username="csrf-relogin")
    _login(client, "csrf-relogin")
    assert client.cookies.get(ACCESS_COOKIE_NAME, path="/api")

    client.cookies.delete(CSRF_COOKIE_NAME)
    resp = client.post(
        "/api/auth/login",
        json={"username": "csrf-relogin", "password": "password"},
    )

    assert resp.status_code == 200, resp.text


def test_register_stays_exempt_while_a_session_cookie_is_present(
    client: TestClient, db
):
    """Same for ``/api/auth/register`` — the second exempt mutating route."""
    make_user(db, username="csrf-register-holder")
    _login(client, "csrf-register-holder")
    client.cookies.delete(CSRF_COOKIE_NAME)

    resp = client.post(
        "/api/auth/register",
        json={
            "username": "csrf-newcomer",
            # RegisterRequest.password_strength 要求長度／大小寫／特殊符號。
            "password": "Csrf-Newcomer1!",
            "email": "csrf-newcomer@example.invalid",
        },
    )

    assert resp.status_code == 201, resp.text


def test_access_log_records_the_routed_path(client: TestClient, db, caplog):
    """The access log has to name the endpoint that actually ran.

    ``app.main._request_access_log`` used to log ``request.url.path`` too,
    so during an attack the log line named a path the router never served
    and an incident reconstructed from it pointed at the wrong endpoint.
    """
    make_user(db, username="csrf-logged")
    _login(client, "csrf-logged")

    # A safe method, so the request gets past CsrfMiddleware (outermost)
    # and actually reaches the access-log middleware (innermost).
    with caplog.at_level(logging.INFO, logger="csp.access"):
        resp = client.get(
            "/api/auth/me", headers={"Host": "attacker.example/health"}
        )
    assert resp.status_code == 200, resp.text

    lines = [r.getMessage() for r in caplog.records if r.name == "csp.access"]
    assert any("/api/auth/me" in line for line in lines), lines
    assert not any("/health/api/auth/me" in line for line in lines), lines


# ---------------------------------------------------------------------------
# The exemption list versus the real route table.
#
# Segment-boundary matching stops a future ``/api/auth/login-as`` inheriting
# the exemption, but it cannot stop ``/api/auth/login/2fa``, which sits under
# the prefix at a genuine boundary. The two guards are complementary: the
# matcher fails safe at runtime, this test fails noisily at CI.
# ---------------------------------------------------------------------------

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Marker for a mounted sub-application whose routes cannot be enumerated.
_OPAQUE = "<opaque-mount>"

# Every mutating route the exemption list is *allowed* to cover. Both are
# pre-session by construction: the caller has no CSRF token to send yet.
EXPECTED_EXEMPT_MUTATING_ROUTES = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/register"),
}


def _walk_routes(routes, prefix: str = ""):
    """Yield ``(method, full_path, route)``, descending into sub-applications.

    A plain ``app.routes`` walk misses containers: they carry no ``methods``,
    so a sub-application mounted under an exempt prefix and serving ``POST``
    would be invisible to a guard that only looks at the top level — while
    ``_should_skip`` exempts it happily. Sub-apps that cannot be enumerated
    (``StaticFiles``, WSGI) are yielded with the ``_OPAQUE`` marker so the
    caller has to decide about them rather than skip them silently.

    Descent is keyed on carrying a ``routes`` attribute rather than on
    ``isinstance(..., Mount)``: ``starlette.routing.Host`` is a container
    too, and an isinstance check walks straight past a host-based sub-app
    serving ``POST /health/wipe``. ``Route`` has no ``routes``, so endpoints
    are unaffected; ``Host`` has no ``path`` either, which is correct —
    host-based routing adds no path prefix.
    """
    for route in routes:
        path = prefix + (getattr(route, "path", "") or "")
        if hasattr(route, "routes"):
            sub = route.routes
            if sub:
                yield from _walk_routes(sub, path)
            else:
                yield (_OPAQUE, path, route)
            continue
        for method in getattr(route, "methods", None) or ():
            yield (method, path, route)


def test_no_unexpected_mutating_route_is_csrf_exempt():
    """Fails the moment a new mutating route collides with an exempt prefix.

    Covers routes registered on the app *and* on any mounted sub-app; a
    mount we cannot enumerate has to be read-only or this fails too. If
    this goes red, a route was added that ``_matches_exempt_prefix``
    covers. That is a decision, not an accident: either the route
    genuinely needs to be exempt (add it here, with a reason) or the
    prefix needs to stop covering it. Do not silence it by widening the
    expected set without saying why.
    """
    from app.main import app as real_app

    exempt_mutating = set()
    opaque_mounts = []
    for method, path, route in _walk_routes(real_app.routes):
        # A mount's own path has no trailing slash ("/static"), while the
        # prefix that covers it does ("/static/"); test both spellings.
        if not (_matches_exempt_prefix(path) or _matches_exempt_prefix(path + "/")):
            continue
        if method == _OPAQUE:
            opaque_mounts.append((path, route))
        elif method in MUTATING_METHODS:
            exempt_mutating.add((method, path))

    assert exempt_mutating == EXPECTED_EXEMPT_MUTATING_ROUTES, (
        "CSRF-exempt mutating routes changed; unexpected="
        f"{sorted(exempt_mutating - EXPECTED_EXEMPT_MUTATING_ROUTES)} "
        f"missing={sorted(EXPECTED_EXEMPT_MUTATING_ROUTES - exempt_mutating)}"
    )

    # Anything mounted under an exempt prefix that cannot be enumerated must
    # be read-only, or the assertion above is blind to whatever it serves.
    for path, route in opaque_mounts:
        assert isinstance(route.app, StaticFiles), (
            f"{path} mounts a non-enumerable, non-read-only app "
            f"({type(route.app).__name__}) under a CSRF-exempt prefix"
        )


def test_walk_routes_sees_every_container_shape(tmp_path):
    """The walker itself, on the shapes it has to survive.

    ``Host`` is the one an ``isinstance(..., Mount)`` check walks past —
    a host-routed sub-app serving ``POST /health/wipe`` would be exempt and
    invisible at the same time.
    """

    async def _endpoint(request):  # pragma: no cover - never called
        raise AssertionError("routing only")

    mounted = Starlette(routes=[Route("/purge", _endpoint, methods=["POST"])])
    hosted = Starlette(routes=[Route("/health/wipe", _endpoint, methods=["POST"])])
    app = Starlette(
        routes=[
            Route("/top", _endpoint, methods=["POST"]),
            Mount("/docs/admin", app=mounted),
            Host("admin.internal", app=hosted),
            Mount("/static", app=StaticFiles(directory=str(tmp_path))),
        ]
    )

    seen = {(method, path) for method, path, _ in _walk_routes(app.routes)}

    assert ("POST", "/top") in seen
    assert ("POST", "/docs/admin/purge") in seen, "Mount not descended into"
    assert ("POST", "/health/wipe") in seen, "Host not descended into"
    assert (_OPAQUE, "/static") in seen, "StaticFiles not reported as opaque"


def _request(host: str, path: str) -> Request:
    """Build the ASGI scope an ASGI server would hand the middleware."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "server": ("csp", 8000),
            "headers": [
                (b"host", host.encode()),
                (
                    b"cookie",
                    f"{ACCESS_COOKIE_NAME}=session; {CSRF_COOKIE_NAME}=v".encode(),
                ),
            ],
        }
    )


@pytest.mark.parametrize("host", POLLUTED_HOSTS)
def test_should_skip_ignores_the_host_header(host):
    """Unit-level companion to the end-to-end cases above."""
    assert _should_skip(_request(host, "/api/users/1/approve")) is False


@pytest.mark.parametrize(
    "path", ["/api/auth/login", "/api/auth/register", "/static/app.js"]
)
def test_should_skip_still_honours_real_exempt_paths(path):
    assert _should_skip(_request("anila.ai.ncsist.org.tw", path)) is True


# ---------------------------------------------------------------------------
# Segment-boundary matching.
#
# Every path that is exempt today must stay exempt — a CSRF fix that breaks
# login is worse than the bypass — while sibling paths that merely share a
# textual prefix must not inherit the exemption.
# ---------------------------------------------------------------------------

STILL_EXEMPT = [
    "/api/auth/login",  # the exempt POST route
    "/api/auth/register",  # the other exempt POST route
    "/api/auth/providers",
    "/api/auth/oidc/",
    "/api/auth/oidc/3/callback",  # trailing-slash entry keeps prefix semantics
    "/health",
    "/docs",
    "/docs/oauth2-redirect",  # a real segment below the entry
    "/openapi.json",
    "/static/",
    "/static/js/app.js",
]

NO_LONGER_EXEMPT = [
    "/api/auth/login-as",
    "/api/auth/login-attempts/17",
    "/api/auth/logins",
    "/api/auth/registered-devices",
    "/api/auth/providers-admin",
    "/healthz",
    "/health-tools/purge",
    "/docs-admin/reindex",
    "/openapi.json.bak",
    "/staticfiles/evil",
]


@pytest.mark.parametrize("path", STILL_EXEMPT)
def test_boundary_matching_keeps_every_current_exemption(path):
    assert _matches_exempt_prefix(path) is True


@pytest.mark.parametrize("path", NO_LONGER_EXEMPT)
def test_boundary_matching_drops_prefix_siblings(path):
    assert _matches_exempt_prefix(path) is False


def test_boundary_matching_does_not_cover_paths_below_an_exempt_route():
    """Stated so nobody mistakes this for the whole answer.

    ``/api/auth/login/2fa`` sits under the exempt prefix at a genuine
    segment boundary, so boundary matching cannot exclude it. That case is
    covered by ``test_no_unexpected_mutating_route_is_csrf_exempt``, which
    goes red when such a route is registered.
    """
    assert _matches_exempt_prefix("/api/auth/login/2fa") is True


# ---------------------------------------------------------------------------
# Keeping the repo-side guard observable after starlette is upgraded.
#
# starlette >= 1.0.1 makes ``request.url.path`` equal ``scope["path"]``, so on
# the version we ship *no* Host header can tell the two apart — every test
# above passes with the bug reintroduced. The repo half exists precisely so
# the boundary does not depend on the library staying correct, so it needs a
# check that does not depend on the library being wrong.
#
# Both levers below substitute the **public** ``url`` property. Nothing
# private is touched: ``HTTPConnection.url`` is where starlette defines it on
# both 0.49.3 and 1.3.1, and overriding a property in a subclass is ordinary
# Python. They stand in for any future library regression — or a downgraded
# starlette — that lets the caller influence the URL again.
# ---------------------------------------------------------------------------


class _PollutedUrlRequest(Request):
    """A ``Request`` whose public ``url`` disagrees with its routed path."""

    @property
    def url(self) -> URL:
        return URL(f"http://attacker.example{EXEMPT_PREFIX}{self.scope['path']}")


@pytest.fixture
def polluted_url(monkeypatch):
    """Make every ``Request.url`` in the app report an exempt prefix."""

    def _fake_url(self) -> URL:
        return URL(f"http://attacker.example{EXEMPT_PREFIX}{self.scope['path']}")

    monkeypatch.setattr(HTTPConnection, "url", property(_fake_url))


def test_should_skip_does_not_consult_request_url_at_all():
    request = _PollutedUrlRequest(
        _request("anila.ai.ncsist.org.tw", "/api/users/1/approve").scope
    )

    # Premise first: if the substitution ever stops taking effect, this test
    # fails loudly instead of passing for the wrong reason.
    assert request.url.path == "/api/auth/login/api/users/1/approve"
    assert request.scope["path"] == "/api/users/1/approve"

    assert _should_skip(request) is False


def test_csrf_holds_through_a_library_regression(client: TestClient, db, polluted_url):
    """End-to-end twin of the test above, on whatever starlette is installed."""
    actor, victim = _make_actor_and_victim(db)
    _login(client, actor.username)

    resp = client.post(PROTECTED_PATH.format(user_id=victim.id))

    assert resp.status_code == 403, resp.text
    db.refresh(victim)
    assert victim.is_approved is False


def test_access_log_holds_through_a_library_regression(
    client: TestClient, db, caplog, polluted_url
):
    """Twin of ``test_access_log_records_the_routed_path``, version-independent."""
    make_user(db, username="csrf-logged-regression")
    _login(client, "csrf-logged-regression")

    with caplog.at_level(logging.INFO, logger="csp.access"):
        resp = client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text

    lines = [r.getMessage() for r in caplog.records if r.name == "csp.access"]
    assert any(" /api/auth/me " in line for line in lines), lines
    assert not any(EXEMPT_PREFIX + "/api/auth/me" in line for line in lines), lines
