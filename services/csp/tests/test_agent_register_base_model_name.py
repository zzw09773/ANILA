"""Registering an agent by base model NAME (not just numeric id).

Why this file exists: ``anila-core register`` — the documented way a
developer mounts an agent — only ever sent ``base_model_name``, while the
endpoint required ``base_model_id``. Every run of the official command was
a guaranteed 422, which is why the platform had zero registered agents.

Covered here:
  * name-only register succeeds, and id-only still succeeds;
  * the three name-resolution failures each say what to do next
    (no match / ambiguous display name / model disabled);
  * neither field supplied is still refused;
  * ``extra="forbid"`` — an undeclared field is refused, not swallowed;
  * the ``version`` → ``agent_version`` alias survives extra="forbid".
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from tests.conftest import login, make_model, make_user


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _model(db, *, name: str, display_name: str | None = None, active: bool = True):
    """A model_registry row whose display name may differ from its name."""
    from app.models.model_registry import ModelRegistry

    m = ModelRegistry(
        name=name,
        display_name=display_name if display_name is not None else name,
        model_type="llm",
        endpoint_url="http://mock-llm:8080",
        is_active=active,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _register(client, token, **overrides):
    payload = {
        "name": "bmn-agent",
        "endpoint_url": "http://agent:9100",
        "description_for_router": "base-model-name resolution test agent",
    }
    payload.update(overrides)
    return client.post("/api/agents/register", headers=_bearer(token), json=payload)


class TestResolveByName:
    def test_register_with_model_name_and_no_id_succeeds(self, client, db):
        make_user(db, username="bmn_dev1", role="developer")
        model = make_model(db, name="bmn-llm-1")
        token = login(client, "bmn_dev1")

        resp = _register(
            client, token, name="bmn-by-name", base_model_name="bmn-llm-1"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["base_model_id"] == model.id

        from app.models.agent import Agent

        row = db.query(Agent).filter(Agent.name == "bmn-by-name").one()
        assert row.base_model_id == model.id

    def test_register_with_id_still_succeeds(self, client, db):
        make_user(db, username="bmn_dev2", role="developer")
        model = make_model(db, name="bmn-llm-2")
        token = login(client, "bmn_dev2")

        resp = _register(client, token, name="bmn-by-id", base_model_id=model.id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["base_model_id"] == model.id

    def test_display_name_resolves_when_registry_name_differs(self, client, db):
        """A developer types what the console shows them, not the slug."""
        make_user(db, username="bmn_dev3", role="developer")
        model = _model(db, name="vendor/bmn-llm-3", display_name="機關通用大模型")
        token = login(client, "bmn_dev3")

        resp = _register(
            client, token, name="bmn-by-display", base_model_name="機關通用大模型"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["base_model_id"] == model.id

    def test_exact_registry_name_wins_over_a_colliding_display_name(self, client, db):
        """``name`` is unique, so an exact hit is never ambiguous."""
        make_user(db, username="bmn_dev4", role="developer")
        target = _model(db, name="bmn-collide", display_name="bmn-collide-shown")
        _model(db, name="bmn-other", display_name="bmn-collide")
        token = login(client, "bmn_dev4")

        resp = _register(
            client, token, name="bmn-exact-wins", base_model_name="bmn-collide"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["base_model_id"] == target.id


class TestNameResolutionFailures:
    def test_unknown_name_says_which_names_exist(self, client, db):
        make_user(db, username="bmn_dev5", role="developer")
        make_model(db, name="bmn-available-llm")
        token = login(client, "bmn_dev5")

        resp = _register(
            client, token, name="bmn-nomatch", base_model_name="不存在的模型"
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "不存在的模型" in detail
        # Actionable: names they could type instead, and the id escape hatch.
        assert "bmn-available-llm" in detail
        assert "base_model_id" in detail

    def test_ambiguous_display_name_lists_the_candidates_with_ids(self, client, db):
        make_user(db, username="bmn_dev6", role="developer")
        a = _model(db, name="bmn-amb-a", display_name="重複顯示名稱")
        b = _model(db, name="bmn-amb-b", display_name="重複顯示名稱")
        token = login(client, "bmn_dev6")

        resp = _register(
            client, token, name="bmn-ambiguous", base_model_name="重複顯示名稱"
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        # Both candidates named, each with the id that disambiguates it.
        assert "bmn-amb-a" in detail and f"id={a.id}" in detail
        assert "bmn-amb-b" in detail and f"id={b.id}" in detail
        assert "base_model_id" in detail

    def test_disabled_model_says_it_is_disabled_and_what_to_do(self, client, db):
        make_user(db, username="bmn_dev7", role="developer")
        _model(db, name="bmn-disabled-llm", active=False)
        make_model(db, name="bmn-enabled-llm")
        token = login(client, "bmn_dev7")

        resp = _register(
            client, token, name="bmn-disabled", base_model_name="bmn-disabled-llm"
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "已停用" in detail
        # Next step: another enabled model, or ask an admin to re-enable it.
        assert "bmn-enabled-llm" in detail
        assert "管理員" in detail

    def test_disabled_model_by_id_also_refused(self, client, db):
        make_user(db, username="bmn_dev8", role="developer")
        model = _model(db, name="bmn-disabled-llm-2", active=False)
        token = login(client, "bmn_dev8")

        resp = _register(
            client, token, name="bmn-disabled-2", base_model_id=model.id
        )
        assert resp.status_code == 400, resp.text
        assert "已停用" in resp.json()["detail"]

    def test_neither_id_nor_name_is_refused_with_a_usable_message(self, client, db):
        make_user(db, username="bmn_dev9", role="developer")
        token = login(client, "bmn_dev9")

        resp = _register(client, token, name="bmn-nobase")
        assert resp.status_code == 422, resp.text
        rendered = resp.text
        assert "base_model_name" in rendered
        assert "base_model_id" in rendered

    def test_id_and_name_pointing_at_different_models_is_refused(self, client, db):
        make_user(db, username="bmn_dev10", role="developer")
        one = make_model(db, name="bmn-pair-1")
        make_model(db, name="bmn-pair-2")
        token = login(client, "bmn_dev10")

        resp = _register(
            client,
            token,
            name="bmn-mismatch",
            base_model_id=one.id,
            base_model_name="bmn-pair-2",
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "bmn-pair-1" in detail and "bmn-pair-2" in detail

    def test_id_and_name_agreeing_is_accepted(self, client, db):
        make_user(db, username="bmn_dev11", role="developer")
        model = make_model(db, name="bmn-agree")
        token = login(client, "bmn_dev11")

        resp = _register(
            client,
            token,
            name="bmn-agrees",
            base_model_id=model.id,
            base_model_name="bmn-agree",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["base_model_id"] == model.id


class TestExtraForbid:
    def test_undeclared_field_is_rejected_not_swallowed(self, client, db):
        """``draft: true`` used to return 200 and register a LIVE agent."""
        make_user(db, username="bmn_dev12", role="developer")
        model = make_model(db, name="bmn-forbid-llm")
        token = login(client, "bmn_dev12")

        resp = _register(
            client,
            token,
            name="bmn-extra",
            base_model_id=model.id,
            draft=True,
        )
        assert resp.status_code == 422, resp.text
        assert "draft" in resp.text

        from app.models.agent import Agent

        assert db.query(Agent).filter(Agent.name == "bmn-extra").first() is None

    def test_version_alias_still_accepted_under_extra_forbid(self, client, db):
        """``version`` is a declared alias of ``agent_version`` — not an extra."""
        make_user(db, username="bmn_dev13", role="developer")
        model = make_model(db, name="bmn-alias-llm")
        token = login(client, "bmn_dev13")

        resp = _register(
            client,
            token,
            name="bmn-alias",
            base_model_id=model.id,
            version="1.2.3",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["agent_version"] == "1.2.3"
