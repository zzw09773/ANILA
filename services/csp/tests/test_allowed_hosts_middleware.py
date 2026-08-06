"""The incoming Host-header allow-list, and the callers it must never block.

Two halves, and the second one is the point:

- A spoofed Host must not reach a route. That is the security half, and it
  is the second layer behind the 2026-08-06 CSRF fix — a Host carrying a
  path segment now dies before ``CsrfMiddleware`` reads it.
- **Every Host that legitimately reaches csp:8000 in this deployment must
  still be served.** That is the control half. The most expensive failure
  shape on this platform is "all containers green, nobody can get in", and
  a Host allow-list produces exactly it: miss the healthcheck's Host and
  the container flaps; miss ``csp`` and every in-network caller 400s while
  nothing looks wrong. So each legitimate value below is its own test with
  its own caller cited, not a set membership assertion.

The behavioural tests drive the **real** ``app.main`` application, rebuilt
under the allow-list **read out of ``infra/compose/platform.yml``**. Two
deliberate choices, both from things that survived a first review:

- A test that assembled its own middleware stack would stay green after
  the registration in ``app.main`` is deleted.
- A test that hardcoded its own copy of the allow-list would stay green
  after somebody edits the compose default — proving things about a
  string no deployment uses.

Beyond blocking and allowing, this file also pins the three things that
made the first version of the feature unsafe to operate: the on-switch
lives in compose (guarded from the other side by
``test_compose_csp_env_passthrough.py``), the "is it on?" signal has to be
emitted where logging actually works, and a malformed pattern has to be
refused at boot rather than on every subsequent request.
"""

from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app.main
from app.config import settings
from app.middleware.cookies import ACCESS_COOKIE_NAME

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLATFORM_YML = _REPO_ROOT / "infra" / "compose" / "platform.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_RUNBOOK = _REPO_ROOT / "docs" / "runbooks" / "intranet-deployment-runbook.md"

# `${ALLOWED_HOSTS:-<default>}`
_COMPOSE_DEFAULT = re.compile(r"^\$\{ALLOWED_HOSTS:-(?P<default>.*)\}$")
# A line in prose that assigns the knob, as an operator would copy it.
_ENV_ASSIGNMENT = re.compile(r"^ALLOWED_HOSTS=(?P<value>.+)$", re.MULTILINE)


def compose_allowlist_default() -> str:
    """The allow-list the deployment actually ships, read from compose.

    Read rather than copied. A literal here would keep every test below
    green after somebody edits `infra/compose/platform.yml`, which is the
    file that decides what production runs — the tests would be proving
    things about a string no deployment uses.
    """
    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    env = doc["services"]["csp"]["environment"]
    assert "ALLOWED_HOSTS" in env, (
        "platform.yml's csp block no longer passes ALLOWED_HOSTS. That line "
        "is the on-switch: app/config.py defaults to '*', so without it the "
        "Host check is off in every deployment while .env still carries a "
        "list that looks live. (test_compose_csp_env_passthrough.py guards "
        "the same line from the other direction.)"
    )
    value = str(env["ALLOWED_HOSTS"])
    match = _COMPOSE_DEFAULT.match(value)
    assert match, (
        f"platform.yml csp.ALLOWED_HOSTS is {value!r} — not the "
        "`${ALLOWED_HOSTS:-...}` shape this file reads it from"
    )
    return match.group("default")


DEPLOYED_ALLOWLIST = compose_allowlist_default()

# What that default is *supposed* to resolve to, spelled out once. This is
# the tripwire that makes a compose edit move a test: change the shipped
# set and this set stops matching, with the diff named in the failure.
EXPECTED_EFFECTIVE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "csp",
    "10.53.100.15",
    "172.16.120.35",
    "*.ncsist.org.tw",
}

# The one value an operator is told to write that is *not* the shipped set:
# the documented way out of a lockout.
RESCUE_VALUE = "*"

# Every Host value recon found a real caller for, with that caller.
# `(host_header_sent, why)` — the header is sent exactly as the caller
# sends it, ports included, because port stripping is part of what is
# under test.
LEGITIMATE_HOSTS = [
    (
        "localhost:8000",
        "csp container healthcheck: "
        "urllib.request.urlopen('http://localhost:8000/health') "
        "— infra/compose/platform.yml, csp.healthcheck",
    ),
    (
        "127.0.0.1",
        "nginx loopback readiness listener proxies /health to csp_backend "
        "with `proxy_set_header Host $host`; its own healthcheck speaks to "
        "127.0.0.1:8080 — infra/nginx/anila.conf + platform.yml nginx.healthcheck",
    ),
    (
        "csp:8000",
        "router / anila-studio / asr-gateway CSP_BASE_URL=http://csp:8000 and "
        "ingestion-worker EMBEDDING_BASE_URL=http://csp:8000/v1 "
        "(infra/compose/platform.yml); also flux2-dev-agent from the "
        "separate anila-models project (infra/models/docker-compose.yml) and "
        "the revocation ping in infra/deployment/scripts/deploy-prod.sh — "
        "five callers, one Host",
    ),
    (
        "anila.ai.ncsist.org.tw",
        "the intranet FQDN (.env.example ANILA_HOST); nginx forwards it "
        "verbatim via `proxy_set_header Host $host`, matched by the "
        "*.ncsist.org.tw wildcard",
    ),
    (
        "aiops.ai.ncsist.org.tw",
        "second FQDN under the same wildcard — the entry is a wildcard on "
        "purpose (nginx map: ~^.+\\.ncsist\\.org\\.tw$), so one more intranet "
        "name must not need a csp restart",
    ),
    (
        "10.53.100.15",
        "the platform host IP; .env.example documents connecting by IP "
        "(with a cert warning) until the DNS A record exists",
    ),
    (
        "172.16.120.35",
        "the trial machine — nginx $is_anila_host allows it and it is the "
        "ANILA_HOST fallback in platform.yml (n8n / gitlab)",
    ),
]

# Hosts that must be rejected. The first four are the 2026-08-06 shape:
# a path segment smuggled into the Host header.
SPOOFED_HOSTS = [
    "attacker.example/api/auth/login",
    "attacker.example/health",
    "evil.example",
    "ncsist.org.tw.attacker.example",
    # The wildcard is `*.ncsist.org.tw`; the bare apex has no caller and
    # must not be inherited (starlette matches on endswith(".ncsist.org.tw")).
    "ncsist.org.tw",
    # 10.53.100.12 is the model gateway, not an ingress: nothing reaches
    # csp with this Host. nginx's map still allows it, so csp is the
    # narrower of the two here — deliberate, see the runbook.
    "10.53.100.12",
]


@pytest.fixture
def allowlisted_app():
    """``app.main`` rebuilt with the deployed allow-list, then put back.

    Reloading the module rather than hand-building a stack is what makes
    these tests sensitive to the production wiring: delete the
    ``install_host_allowlist(app, settings.ALLOWED_HOSTS)`` call and the
    spoofed-Host cases below go red.

    Function-scoped deliberately. ``app.main`` is process-wide state, and a
    module-scoped rebuild stays in place until the last test in the file —
    long enough to make the "the default registers nothing" tests below read
    the *rebuilt* module and fail. Scoping per test means no test in this
    file can be made to pass or fail by where it sits in the file.
    """
    original_setting = settings.ALLOWED_HOSTS
    original_module_app = app.main.app
    settings.ALLOWED_HOSTS = DEPLOYED_ALLOWLIST
    try:
        importlib.reload(app.main)
        # Premise check: if the reload silently produced a permissive app,
        # every assertion below would pass for the wrong reason.
        assert app.main._allowed_hosts != ["*"], app.main._allowed_hosts
        yield app.main.app
    finally:
        settings.ALLOWED_HOSTS = original_setting
        importlib.reload(app.main)
        app.main.app = original_module_app


@pytest.fixture
def allowlisted_client(allowlisted_app):
    # No context manager: entering TestClient runs the lifespan, which runs
    # alembic. /health needs neither.
    return TestClient(allowlisted_app)


# ── The control case: nothing legitimate may be blocked ────────────────────


@pytest.mark.parametrize(
    "host,caller", LEGITIMATE_HOSTS, ids=[h for h, _ in LEGITIMATE_HOSTS]
)
def test_legitimate_host_is_served(allowlisted_client, host, caller):
    """One caller per test. A red line here is a lockout, not a nit."""
    resp = allowlisted_client.get("/health", headers={"Host": host})

    assert resp.status_code == 200, (
        f"Host {host!r} was rejected — this locks out: {caller}"
    )
    assert resp.json()["status"] == "healthy"


def test_every_legitimate_host_survives_a_narrowed_allowlist():
    """Narrowing ALLOWED_HOSTS must not be able to cut internal plumbing.

    The realistic operator mistake is setting ALLOWED_HOSTS to just the
    FQDN. That would blind the container healthcheck, and
    ``depends_on: csp: service_healthy`` would then keep nginx from
    starting at all — a total outage from one plausible edit.
    """
    hosts = app.main.parse_allowed_hosts("anila.ai.ncsist.org.tw")

    for internal in app.main._INTERNAL_HOSTS:
        assert internal in hosts, (
            f"{internal!r} dropped out of a narrowed allow-list"
        )


# ── The security case ──────────────────────────────────────────────────────


@pytest.mark.parametrize("host", SPOOFED_HOSTS)
def test_spoofed_host_is_rejected(allowlisted_client, host):
    resp = allowlisted_client.get("/health", headers={"Host": host})

    assert resp.status_code == 400, resp.text
    assert "Invalid host header" in resp.text


def test_host_check_runs_outside_the_csrf_middleware(allowlisted_app):
    """Ordering, stated as the invariant rather than as a line number.

    starlette runs the most recently added middleware first, so the
    allow-list must sit ahead of ``CsrfMiddleware`` in ``user_middleware``.
    Behind it, a poisoned Host would still be read by the layer it fooled
    on 2026-08-06 before anything rejected it.
    """
    from app.middleware.csrf import CsrfMiddleware

    classes = [m.cls for m in allowlisted_app.user_middleware]

    assert app.main.HostAllowlistMiddleware in classes, "allow-list not registered"
    assert CsrfMiddleware in classes, "CSRF middleware disappeared"
    assert classes.index(app.main.HostAllowlistMiddleware) < classes.index(
        CsrfMiddleware
    ), (
        f"allow-list runs inside CsrfMiddleware; order={classes}"
    )


def test_a_spoofed_host_dies_before_csrf_reports_on_it(allowlisted_client):
    """End-to-end twin of the ordering test.

    A mutating request with a path-carrying Host, a session cookie and no
    CSRF token. The session cookie is what makes this discriminating:
    ``CsrfMiddleware`` skips cookie-less requests entirely
    (``app/middleware/csrf.py``), so without one the answer is 400 no
    matter which order the two middlewares sit in, and the test would pass
    while proving nothing. With the cookie, CSRF has an opinion — 403 — so
    a 400 means the allow-list genuinely ran first. No DB is touched: the
    cookie only has to exist.
    """
    resp = allowlisted_client.post(
        "/api/users/1/approve",
        headers={
            "Host": "attacker.example/api/auth/login",
            "Cookie": f"{ACCESS_COOKIE_NAME}=not-a-real-session",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "Invalid host header" in resp.text


def test_the_csrf_verdict_is_what_the_previous_test_displaces(allowlisted_client):
    """Premise of the test above, asserted rather than assumed.

    Same request with an allowed Host: CSRF must answer 403. If this ever
    stops being 403, the 400 next door stops meaning "the allow-list won
    the race" and starts meaning nothing.
    """
    resp = allowlisted_client.post(
        "/api/users/1/approve",
        headers={
            "Host": "anila.ai.ncsist.org.tw",
            "Cookie": f"{ACCESS_COOKIE_NAME}=not-a-real-session",
        },
    )

    assert resp.status_code == 403, resp.text


# ── The default: safe, but never self-locking ──────────────────────────────


def test_fresh_settings_do_not_lock_out_anyone():
    """A default ``Settings()`` must not be able to strand a caller.

    The library default is permissive on purpose (see the comment on
    ``Settings.ALLOWED_HOSTS``): the deployment turns the check on, so the
    default's only job is to be impossible to lock yourself out with.
    """
    from app.config import Settings

    assert Settings().ALLOWED_HOSTS == "*"
    assert app.main.parse_allowed_hosts(Settings().ALLOWED_HOSTS) == ["*"]


def test_the_default_app_registers_no_host_check():
    """...and that default really does leave the middleware off.

    Stated so the permissive default cannot rot into a surprise: the whole
    test suite reaches the app as ``Host: testserver``, which no allow-list
    here contains.
    """
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    assert app.main._allowed_hosts == ["*"]
    # Both spellings: the subclass is what we register, the base class is
    # what a future refactor might go back to. Neither may be present.
    registered = [m.cls for m in app.main.app.user_middleware]
    assert app.main.HostAllowlistMiddleware not in registered
    assert TrustedHostMiddleware not in registered


def test_default_configuration_serves_the_docker_internal_callers(client):
    """The docker-internal Hosts work on the default app too.

    Requirement in its own right: whatever the default is, a fresh
    ``up -d`` that somehow missed the compose plumbing must still let
    router / studio / worker / the healthcheck through.
    """
    for host in ("localhost:8000", "127.0.0.1", "csp:8000"):
        resp = client.get("/health", headers={"Host": host})
        assert resp.status_code == 200, (host, resp.text)


# ── Parsing ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["*", "", "   ", None, "a.example,*", ",,"])
def test_disabled_sentinels_collapse_to_star(raw):
    """Anything meaning "no opinion" must disable, not half-enable.

    ``"a.example,*"`` included: a list containing a wildcard entry is an
    operator saying "allow anything", and half-honouring it would enable a
    check they did not ask for.
    """
    assert app.main.parse_allowed_hosts(raw) == ["*"]


def test_whitespace_and_ordering_are_tolerated():
    hosts = app.main.parse_allowed_hosts(" a.example , b.example ")

    assert hosts[:2] == ["a.example", "b.example"]
    assert set(app.main._INTERNAL_HOSTS) <= set(hosts)


def test_internal_hosts_are_not_duplicated():
    """An operator who lists them explicitly must not get them twice."""
    hosts = app.main.parse_allowed_hosts("csp,localhost,a.example")

    assert hosts.count("csp") == 1
    assert hosts.count("localhost") == 1


def test_port_bearing_entries_are_not_silently_accepted():
    """A documented footgun, pinned.

    ``TrustedHostMiddleware`` compares ``host.split(":")[0]``, so an entry
    written as ``csp:8000`` can never match anything. The parser does not
    rewrite it — that would be guessing — so this test exists to make the
    behaviour visible if someone puts a port in .env.example.
    """
    hosts = app.main.parse_allowed_hosts("example.internal:8443")

    assert "example.internal:8443" in hosts
    assert "example.internal" not in hosts


# ── Host spelling: case and the DNS root dot ───────────────────────────────
#
# Hostnames are case-insensitive and the trailing dot is optional, so these
# are the *same name* as entries already on the list. nginx folds case into
# `$host`, but several csp locations forward `$http_host` (the raw header),
# and anything reaching csp:8000 directly is unfiltered — so csp sees both
# spellings and must give both the same verdict.

SAME_NAME_DIFFERENT_SPELLING = [
    "ANILA.AI.NCSIST.ORG.TW",
    "anila.ai.ncsist.org.tw.",
    "ANILA.AI.NCSIST.ORG.TW.",
    "LOCALHOST:8000",
    "CSP",
    "localhost.",
]

# Spellings that merely *look* related. The point of normalising is that it
# must not blur these into the allow-list.
STILL_STRANGERS = [
    "ncsist.org.tw.evil.com",
    "NCSIST.ORG.TW.EVIL.COM",
    "evil.com:csp",
    "csp.evil.com",
    "xncsist.org.tw",
    "localhost.evil.com",
]


@pytest.mark.parametrize("host", SAME_NAME_DIFFERENT_SPELLING)
def test_the_same_name_spelled_differently_is_served(allowlisted_client, host):
    resp = allowlisted_client.get("/health", headers={"Host": host})

    assert resp.status_code == 200, (
        f"Host {host!r} is the same name as an allow-listed entry, "
        "spelled per RFC — rejecting it is a lockout"
    )


@pytest.mark.parametrize("host", STILL_STRANGERS)
def test_normalisation_does_not_blur_in_a_stranger(allowlisted_client, host):
    resp = allowlisted_client.get("/health", headers={"Host": host})

    assert resp.status_code == 400, resp.text


def test_an_empty_host_is_still_rejected(allowlisted_client):
    resp = allowlisted_client.get("/health", headers={"Host": ""})

    assert resp.status_code == 400, resp.text


def test_allowlist_entries_are_normalised_too():
    """An operator who types the FQDN in capitals gets what they meant."""
    hosts = app.main.parse_allowed_hosts("ANILA.AI.NCSIST.ORG.TW.,*.NCSIST.ORG.TW")

    assert "anila.ai.ncsist.org.tw" in hosts
    assert "*.ncsist.org.tw" in hosts


def test_normalize_host_leaves_an_ipv6_literal_alone():
    """Not a feature — a pinned non-change.

    ``[::1]:8000`` has colons inside the host part, so neither starlette's
    ``split(":")[0]`` nor this normalisation handles it as an address.
    Both leave it rejected; this test says that is known, not accidental.
    """
    assert app.main.normalize_host("[::1]:8000") == "[::1]:8000"


# ── A malformed pattern must refuse at boot, not on every request ──────────


MALFORMED_PATTERNS = [
    "10.53.*.15",       # the "cover the subnet" typo
    "*ncsist.org.tw",   # missing the dot after the star
    "*.ncsist.*.tw",    # two stars
    "10.53.100.*",
]


@pytest.mark.parametrize("pattern", MALFORMED_PATTERNS)
def test_a_malformed_pattern_is_refused_and_named(pattern):
    """Fail-fast, and say which entry is wrong.

    starlette validates patterns with ``assert`` inside ``__init__``, and
    FastAPI builds middleware lazily — so without this the app registers
    fine and raises on the *first request*, ``/health`` included: the
    container goes unhealthy and `depends_on: csp: service_healthy` stops
    nginx from ever starting. A typo becoming a total outage with no error
    naming it is the shape this rejects.
    """
    with pytest.raises(ValueError) as excinfo:
        app.main.parse_allowed_hosts(f"anila.ai.ncsist.org.tw,{pattern}")

    assert pattern in str(excinfo.value), (
        f"the error does not name the offending pattern: {excinfo.value}"
    )


def test_a_malformed_pattern_never_reaches_a_registered_middleware():
    """The refusal has to happen before anything is wired up."""
    from fastapi import FastAPI

    victim = FastAPI()
    with pytest.raises(ValueError):
        app.main.install_host_allowlist(victim, "10.53.*.15")

    assert victim.user_middleware == [], (
        "a middleware was registered despite the malformed pattern"
    )


def test_the_shipped_default_survives_validation():
    """Control: the guard must not reject the value we actually ship."""
    assert set(app.main.parse_allowed_hosts(DEPLOYED_ALLOWLIST)) == (
        EXPECTED_EFFECTIVE_HOSTS
    )


def test_the_rescue_value_survives_validation():
    """`*` alone is the documented way out and must never be 'malformed'."""
    assert app.main.parse_allowed_hosts(RESCUE_VALUE) == ["*"]


# ── The "is it on?" signal the runbook greps for ───────────────────────────


def _tagged_records(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if app.main.HOST_ALLOWLIST_LOG_TAG in r.getMessage()
    ]


def test_the_enforced_state_is_announced_with_its_hosts(caplog):
    hosts = app.main.parse_allowed_hosts(DEPLOYED_ALLOWLIST)

    with caplog.at_level(logging.INFO, logger="csp"):
        app.main.log_host_allowlist_state(hosts)

    lines = _tagged_records(caplog)
    assert len(lines) == 1, lines
    assert "ENFORCED" in lines[0]
    for host in EXPECTED_EFFECTIVE_HOSTS:
        assert host in lines[0], f"{host} missing from the announcement"


def test_the_disabled_state_is_announced_too(caplog):
    """Silence must not be the only way to say "off".

    If only the ON branch logged, an operator grepping and finding nothing
    could not tell "the check is off" from "the boot never got there" —
    which is exactly the false negative this replaced.
    """
    with caplog.at_level(logging.INFO, logger="csp"):
        app.main.log_host_allowlist_state(["*"])

    lines = _tagged_records(caplog)
    assert len(lines) == 1, lines
    assert "DISABLED" in lines[0]


def test_the_disabled_state_is_loud_enough_to_survive_a_terse_log_level():
    """Off is the weaker security posture, so it is a warning, not info."""
    record_levels = []

    class _Capture(logging.Handler):
        def emit(self, record):
            record_levels.append(record.levelno)

    log = logging.getLogger("csp")
    handler = _Capture()
    log.addHandler(handler)
    try:
        app.main.log_host_allowlist_state(["*"])
    finally:
        log.removeHandler(handler)

    assert record_levels and max(record_levels) >= logging.WARNING


def test_the_announcement_happens_where_logging_actually_works():
    """It must be called from the lifespan, not at import.

    ``setup_logging`` runs inside the lifespan; anything logged during
    ``import app.main`` goes to a handler-less root logger at WARNING and
    is dropped. This asserts the call site, because the failure it guards
    against is invisible in behaviour — the line simply never appears.
    """
    import inspect

    source = inspect.getsource(app.main.lifespan)

    assert "log_host_allowlist_state" in source, (
        "the state announcement is not called from the lifespan — if it "
        "moved back to import time the runbook check silently returns nothing"
    )
    # ...and after the setup_logging() that repairs what alembic tore down.
    assert source.index("log_host_allowlist_state") > source.rindex(
        "setup_logging()"
    ), "announced before the last setup_logging(); alembic disables handlers"


# ── One set, many files: keep the copies from drifting ─────────────────────


def test_the_documented_copies_all_match_the_compose_default():
    """.env.example and the runbook carry the same list; nobody re-types it.

    Acceptance counted six hand-synced copies of this set. This collapses
    the ones inside the repo's own docs into machine-checked ones, so the
    remaining hand-sync is compose ⇄ nginx's `$is_anila_host` map — called
    out in the runbook, and not parseable from here without an nginx
    config parser (deliberately out of scope).
    """
    for path in (_ENV_EXAMPLE, _RUNBOOK):
        text = path.read_text(encoding="utf-8")
        found = _ENV_ASSIGNMENT.findall(text)
        assert found, f"{path.name} no longer documents ALLOWED_HOSTS at all"
        for value in found:
            assert value in (DEPLOYED_ALLOWLIST, RESCUE_VALUE), (
                f"{path.name} documents ALLOWED_HOSTS={value!r}, but compose "
                f"ships {DEPLOYED_ALLOWLIST!r} — an operator copying the docs "
                "would get a different allow-list than a default `up -d`"
            )


def test_the_runbook_documents_both_the_signal_and_the_way_out():
    """The two things an operator needs when this bites at 3am."""
    text = _RUNBOOK.read_text(encoding="utf-8")

    assert app.main.HOST_ALLOWLIST_LOG_TAG in text, (
        "the runbook greps for a log tag that is not the one the app emits"
    )
    assert "ENFORCED" in text and "DISABLED" in text, (
        "the runbook does not show both verdicts of the check"
    )
