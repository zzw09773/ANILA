"""Download-bundle contract tests (design 2026-09-22 §6).

Two endpoints, two artifacts, both asserted by **reading the produced zip**:

* ``GET /api/agents/template/download``         → quickstart scaffold (default)
* ``GET /api/agents/examples/advanced/download`` → advanced example (packages/anila-agent)

The feature is fail-closed, so a large part of this module is about what must
NOT arrive: no secrets, no VCS dirs, no escaping symlinks, no "可直接啟動" zip
when the site profile, hash lock, or lab image version is missing. The old
endpoint used to hardcode ``anila-agent.zip``; each endpoint must now name
itself, or a developer cannot tell which project they downloaded.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from tests.conftest import (
    login,
    make_agent,
    make_model,
    make_user,
)
from tests.quickstart_fixtures import (
    SCAFFOLD_FILE_BODIES,
    build_all_inputs,
    build_profile,
    build_scaffold,
    make_wheel,
)

_QUICKSTART_URL = "/api/agents/template/download"
_ADVANCED_URL = "/api/agents/examples/advanced/download"
_VERIFIER = b'"""canonical verifier fixture"""\nVERIFY = True\n'


def _stub_verifier(monkeypatch, path):
    path.write_bytes(_VERIFIER)
    monkeypatch.setattr("app.api.agents.registration._ANILA_VERIFY_SOURCE", path)
    return path


def _configure(monkeypatch, inputs):
    """Point every bundle input constant at the throwaway tree."""
    monkeypatch.setattr(
        "app.api.agents.registration._QUICKSTART_DIR", inputs["scaffold"]
    )
    monkeypatch.setattr(
        "app.api.agents.registration._QUICKSTART_PROFILE", inputs["profile"]
    )
    monkeypatch.setattr("app.api.agents.registration._TEMPLATE_DIR", inputs["advanced"])
    _stub_verifier(monkeypatch, inputs["advanced"] / "verifier-source.py")


def _get(client, token, url, **params):
    return client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)


def _zip_of(response) -> zipfile.ZipFile:
    assert response.content, "empty 200 body"
    return zipfile.ZipFile(io.BytesIO(response.content))


def _bind(db, agent, ids: list[int]) -> None:
    """Replace an agent's collection bindings (junction is the source of truth)."""
    from app.models.agent import AgentCollectionBinding

    agent.collection_bindings.clear()
    db.flush()
    for cid in ids:
        agent.collection_bindings.append(AgentCollectionBinding(collection_id=cid))
    agent.bound_collection_id = sorted(ids)[0] if ids else None
    db.commit()
    db.refresh(agent)


@pytest.mark.parametrize("url", [_QUICKSTART_URL, _ADVANCED_URL])
def test_plain_user_cannot_download(client, db, url):
    name = "user_dl_" + url.strip("/").split("/")[2].replace("/", "_")
    make_user(db, username=name, role="user")
    token = login(client, name)
    assert _get(client, token, url).status_code == 403


@pytest.mark.parametrize("url", [_QUICKSTART_URL, _ADVANCED_URL])
def test_unauthenticated_cannot_download(client, url):
    assert client.get(url).status_code == 401


def test_foreign_agent_id_is_refused(client, db, monkeypatch, tmp_path):
    """A developer must not be able to download another owner's deployment資料."""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    owner = make_user(db, username="owner_dl", role="developer")
    make_user(db, username="other_dl", role="developer")
    agent = make_agent(db, owner=owner, name="owned-agent")

    # Same refusal as GET /api/agents/{id}: 404, so a foreign id is not an
    # existence oracle. (The developer/admin gate is a separate 403 above it.)
    token = login(client, "other_dl")
    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 404
    assert "Agent 不存在" in resp.json()["detail"]

    token = login(client, "owner_dl")
    assert _get(client, token, _QUICKSTART_URL, agent_id=agent.id).status_code == 200


def test_admin_can_download_another_owners_agent(client, db, monkeypatch, tmp_path):
    """Admin-tier is a superset of owner — otherwise the console cannot help."""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    owner = make_user(db, username="owner_admin_dl", role="developer")
    make_user(db, username="admin_dl", role="admin")
    agent = make_agent(db, owner=owner, name="admin-target-agent")
    token = login(client, "admin_dl")
    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert f"ANILA_AGENT_ID={agent.id}" in env_text


def test_plain_user_is_403_even_with_valid_agent_id(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="owner_plain", role="developer")
    make_user(db, username="plain_dl", role="user")
    agent = make_agent(db, owner=_user(db, "owner_plain"), name="plain-agent")
    token = login(client, "plain_dl")
    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 403


def _user(db, username):
    from app.models.user import User

    return db.query(User).filter(User.username == username).first()


def test_missing_agent_id_is_404(client, db, monkeypatch, tmp_path):
    _configure(monkeypatch, build_all_inputs(tmp_path))
    make_user(db, username="dev_missing_agent", role="developer")
    token = login(client, "dev_missing_agent")
    assert _get(client, token, _QUICKSTART_URL, agent_id=98765).status_code == 404


def test_collection_id_requires_agent_id(client, db, monkeypatch, tmp_path):
    _configure(monkeypatch, build_all_inputs(tmp_path))
    make_user(db, username="dev_coll_no_agent", role="developer")
    token = login(client, "dev_coll_no_agent")
    resp = _get(client, token, _QUICKSTART_URL, collection_id=3)
    assert resp.status_code == 400
    assert "agent_id" in resp.json()["detail"]


def test_collection_id_outside_bound_set_is_400(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    dev = make_user(db, username="dev_coll_oob", role="developer")
    model = make_model(db, name="m-oob")
    build_profile(inputs["profile"], ca_path=inputs["ca"], model_keys=(str(model.id),))
    _configure(monkeypatch, inputs)
    agent = make_agent(db, owner=dev, name="bound-agent")
    agent.base_model_id = model.id
    db.commit()
    _bind(db, agent, [4])

    token = login(client, "dev_coll_oob")
    ok = _get(client, token, _QUICKSTART_URL, agent_id=agent.id, collection_id=4)
    assert ok.status_code == 200
    bad = _get(client, token, _QUICKSTART_URL, agent_id=agent.id, collection_id=77)
    assert bad.status_code == 400
    assert "77" in bad.json()["detail"]


# ── quickstart: contents ─────────────────────────────────────────────────────


def test_developer_downloads_named_quickstart_zip(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_quickstart", role="developer")
    token = login(client, "dev_quickstart")

    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    final = resp.headers.get("content-disposition", "")
    assert "filename=anila-agent.zip" not in final
    assert "py313-linux-x86_64.zip" in final

    with _zip_of(resp) as zf:
        names = set(zf.namelist())
        for name in SCAFFOLD_FILE_BODIES:
            assert f"anila-agent-quickstart/{name}" in names
        # The canonical verifier ships by exact bytes into the zip (no second
        # hand-maintained copy under version control).
        assert zf.read("anila-agent-quickstart/anila_verify.py") == _VERIFIER
        assert zf.read("anila-agent-quickstart/deployment.env")
        # A real runnable project, not just docs.
        assert any(n.endswith("server.py") for n in names)
        assert not any("wheelhouse" in n for n in names)
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
        assert manifest["compatible_lab_image_version"] == "1"
        assert manifest["wheelhouse_included"] is False


def test_no_secrets_or_vcs_dirs_in_quickstart_zip(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    scaffold = inputs["scaffold"]
    # Things that must be excluded / included and would leak if allow-list were
    # a plain rglob + denylist.
    (scaffold / ".git").mkdir()
    (scaffold / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (scaffold / "__pycache__").mkdir()
    (scaffold / "__pycache__" / "junk.pyc").write_text("")
    (scaffold / ".env").write_text("LLM_API_KEY=super-secret\n")
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_quickstart_secrets", role="developer")
    token = login(client, "dev_quickstart_secrets")

    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = set(zf.namelist())
    assert not any("/.git/" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)
    # The site .env is excluded; the generated deployment.env is present and
    # carries no secret.
    assert "anila-agent-quickstart/.env" not in names
    assert "anila-agent-quickstart/deployment.env" in names
    assert "super-secret" not in resp.content.decode("latin-1")
    assert b"PRIVATE KEY" not in resp.content


def test_verifier_bytes_match_download_endpoint(client, db, monkeypatch, tmp_path):
    """Release gate: the vendored copy must equal the served canonical source."""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_verifier_match", role="developer")
    token = login(client, "dev_verifier_match")

    zip_bytes = _get(client, token, _QUICKSTART_URL).content
    served = _get(client, token, "/api/agents/anila-verify/download").content
    assert served == _VERIFIER
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.read("anila-agent-quickstart/anila_verify.py") == served


def test_prefilled_agent_name_and_collection(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    dev = make_user(db, username="dev_prefill", role="developer")
    model = make_model(db, name="m-prefill")
    build_profile(inputs["profile"], ca_path=inputs["ca"], model_keys=(str(model.id),))
    _configure(monkeypatch, inputs)
    agent = make_agent(db, owner=dev, name="hr-policy-helper")
    agent.base_model_id = model.id
    db.commit()
    _bind(db, agent, [9])
    token = login(client, "dev_prefill")

    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        agent_src = zf.read("anila-agent-quickstart/agent.py").decode()
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
    assert 'AGENT_NAME = "hr-policy-helper"' in agent_src
    # Only the single bound collection is prefilled; comments are preserved.
    assert "COLLECTION_ID: int | None = 9" in agent_src
    assert "註冊名不可變" in agent_src
    assert f"ANILA_AGENT_ID={agent.id}\n" in env_text
    assert "\nLLM_MODEL=m-prefill\n" in env_text
    assert "LLM_BASE_URL=https://anila.test/v1\n" in env_text
    assert not any(line.startswith("LLM_API_KEY=") for line in env_text.splitlines())
    assert manifest["agent"]["collection_id"] == 9
    assert manifest["agent"]["bound"] is True


def test_multi_bound_leaves_collection_unset(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    dev = make_user(db, username="dev_multi_bind", role="developer")
    model = make_model(db, name="m-multi")
    build_profile(inputs["profile"], ca_path=inputs["ca"], model_keys=(str(model.id),))
    _configure(monkeypatch, inputs)
    agent = make_agent(db, owner=dev, name="multi-agent")
    agent.base_model_id = model.id
    db.commit()
    _bind(db, agent, [3, 5])
    token = login(client, "dev_multi_bind")

    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    with _zip_of(resp) as zf:
        agent_src = zf.read("anila-agent-quickstart/agent.py").decode()
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
    assert "COLLECTION_ID: int | None = None" in agent_src
    assert manifest["agent"]["collection_prefilled"] is False
    assert manifest["agent"]["available_collection_ids"] == [3, 5]


def test_generic_bundle_is_marked_unbound(client, db, monkeypatch, tmp_path):
    """No agent_id → readable but not dispatchable; Console must be able to say so."""
    _configure(monkeypatch, build_all_inputs(tmp_path))
    make_user(db, username="dev_generic", role="developer")
    token = login(client, "dev_generic")

    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert manifest["agent"]["bound"] is False
    assert env_text.rstrip().endswith("LLM_AUTH_REQUIRED=true")
    assert "ANILA_AGENT_ID=\n" in env_text


def test_deployment_env_never_derives_host_or_reuses_dispatch_jwt(
    client, db, monkeypatch, tmp_path
):
    """CSP_BASE_URL comes from the profile, never from the request Host.

    The model call goes to that same origin's ``/v1``. A legacy ``models``
    section in the profile must not leak an internal address into the zip.
    """
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_env_host", role="developer")
    token = login(client, "dev_env_host")

    resp = client.get(
        _QUICKSTART_URL,
        headers={"Authorization": f"Bearer {token}", "Host": "wrong.example:9999"},
    )
    with _zip_of(resp) as zf:
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert "CSP_BASE_URL=https://anila.test" in env_text
    assert "wrong.example" not in env_text
    assert "LLM_BASE_URL=https://anila.test/v1" in env_text
    assert "llm.test" not in env_text
    assert "ncsist/" not in env_text
    assert "\nLLM_MODEL=\n" in env_text


# ── quickstart: fail-closed 503s ─────────────────────────────────────────────


def _prep_dev(client, db, name):
    make_user(db, username=name, role="developer")
    return login(client, name)


def test_missing_profile_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    monkeypatch.setattr(
        "app.api.agents.registration._QUICKSTART_PROFILE",
        tmp_path / "nope.json",
    )
    token = _prep_dev(client, db, "dev_no_profile")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "profile" in resp.json()["detail"]


def test_zip_omits_wheelhouse_even_when_wheels_exist(client, db, monkeypatch, tmp_path):
    """§12: a wheel directory beside the scaffold is not a download input."""
    inputs = build_all_inputs(tmp_path)
    assert any(inputs["wheels"].glob("*.whl"))
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_no_wheels")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
    assert not any("wheelhouse" in n or n.endswith(".whl") for n in names)
    assert manifest["compatible_lab_image_version"] == "1"
    assert manifest["wheelhouse_included"] is False
    assert manifest["requirements_lock_sha256"]


def test_requirements_in_not_covered_by_lock_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    from tests.quickstart_fixtures import REQUIREMENTS_IN

    (inputs["scaffold"] / "requirements.in").write_text(
        REQUIREMENTS_IN + "uvicorn==9.9.9\n", encoding="utf-8"
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_incomplete_wheels")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "uvicorn" in resp.json()["detail"]


def test_hashed_lock_does_not_consult_a_wheel_directory(client, db, monkeypatch, tmp_path):
    """A rewritten hash is still a hash. There is no wheel file to compare it to."""
    inputs = build_all_inputs(tmp_path)
    lock = (inputs["scaffold"] / "requirements.lock").read_text(encoding="utf-8")
    (inputs["scaffold"] / "requirements.lock").write_text(
        lock.replace("--hash=sha256:", "--hash=sha256:").replace(
            lock.split("--hash=sha256:")[1][:64], "b" * 64
        ),
        encoding="utf-8",
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_hash_mismatch")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
    assert not any(n.endswith(".whl") for n in names)
    assert "b" * 64 in (inputs["scaffold"] / "requirements.lock").read_text(encoding="utf-8")
    assert manifest["lock_requirements_with_hash"] >= 1


def test_unhashed_lock_requirement_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    lock = (inputs["scaffold"] / "requirements.lock").read_text(encoding="utf-8")
    lock = lock.replace(" --hash=sha256:" + lock.split("--hash=sha256:")[1][:64], "", 1)
    (inputs["scaffold"] / "requirements.lock").write_text(lock, encoding="utf-8")
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_unhashed_lock")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "hash" in resp.json()["detail"]


def test_zip_does_not_package_wheels_of_any_abi(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    make_wheel(inputs["wheels"], "uvicorn", "9.9.9", pytag="cp311", plattag="manylinux_2_17_x86_64")
    lock = (inputs["scaffold"] / "requirements.lock").read_text(encoding="utf-8")
    lock += f"uvicorn==9.9.9 \\\n    --hash=sha256:{'0' * 64}\n"
    (inputs["scaffold"] / "requirements.lock").write_text(lock, encoding="utf-8")
    (inputs["scaffold"] / "requirements.in").write_text(
        "fastapi==1.2.3\ncryptography==2.0.0\nuvicorn==9.9.9\n", encoding="utf-8"
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_wrong_abi")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
    assert manifest["target_abi"] == "py313-linux-x86_64"
    assert not any("cp311" in n or n.endswith(".whl") for n in names)


def test_missing_required_scaffold_file_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    (inputs["scaffold"] / "server.py").unlink()
    (inputs["scaffold"] / "llm.py").unlink()
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_missing_scaffold")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    # The pre-flight check reports *every* missing file at once, so ops does not
    # discover them one redeploy at a time.
    assert "server.py" in detail and "llm.py" in detail


def test_directory_where_a_scaffold_file_belongs_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    (inputs["scaffold"] / "llm.py").unlink()
    (inputs["scaffold"] / "llm.py").mkdir()
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_dir_scaffold")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "llm.py" in resp.json()["detail"]


def test_missing_image_version_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    (inputs["scaffold"] / "IMAGE_VERSION").unlink()
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_no_image_version")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "IMAGE_VERSION" in resp.json()["detail"]


def test_blank_image_version_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    (inputs["scaffold"] / "IMAGE_VERSION").write_text("  \n", encoding="utf-8")
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_blank_image_version")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "IMAGE_VERSION" in resp.json()["detail"]


def test_missing_verifier_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    monkeypatch.setattr(
        "app.api.agents.registration._ANILA_VERIFY_SOURCE",
        tmp_path / "gone.py",
    )
    token = _prep_dev(client, db, "dev_no_verifier")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "驗證器" in resp.json()["detail"]


def test_empty_verifier_is_503(client, db, monkeypatch, tmp_path):
    """A zero-byte verifier would ship a zip whose auth layer imports nothing."""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    empty = tmp_path / "empty.py"
    empty.write_bytes(b"")
    monkeypatch.setattr("app.api.agents.registration._ANILA_VERIFY_SOURCE", empty)
    token = _prep_dev(client, db, "dev_empty_verifier")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "空檔" in resp.json()["detail"]


def test_escaping_symlink_in_scaffold_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "server.py").write_text("# outside\n")
    (inputs["scaffold"] / "server.py").unlink()
    (inputs["scaffold"] / "server.py").symlink_to(outside / "server.py")
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_symlink_escape")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "symlink" in resp.json()["detail"]


def test_dangling_symlink_beside_scaffold_is_not_packaged(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    (inputs["wheels"] / "ghost-1.0.0-py3-none-any.whl").symlink_to(
        tmp_path / "not-there.whl"
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_dangling_wheel")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = zf.namelist()
    assert not any("ghost" in n for n in names)


def test_ca_with_private_key_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    secret = tmp_path / "chain-with-key.pem"
    secret.write_bytes(inputs["ca_bytes"] + b"-----BEGIN PRIVATE KEY-----\nAA==\n")
    build_profile(inputs["profile"], ca_path=secret)
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_ca_key")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "私鑰" in resp.json()["detail"]


def test_origin_userinfo_is_refused_without_echoing_the_secret(
    client, db, monkeypatch, tmp_path
):
    """A profile origin must not carry username/password into the zip or the error."""
    inputs = build_all_inputs(tmp_path)
    secret = "s3cret-token"
    inputs["profile"].write_text(
        json.dumps(
            {
                "csp_base_url": f"https://lab-user:{secret}@anila.test",
                "csp_ca_file": str(inputs["ca"]),
            }
        ),
        encoding="utf-8",
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_origin_userinfo")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "帳號" in detail or "userinfo" in detail
    assert secret not in detail
    assert secret not in resp.content.decode("utf-8", "replace")
    assert "lab-user" not in detail


def test_unreadable_ca_is_503_not_500(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    ca = inputs["ca"]
    ca.chmod(0)
    try:
        try:
            ca.read_bytes()
        except OSError:
            pass
        else:
            pytest.skip("this uid can still read a mode 000 file")
        _configure(monkeypatch, inputs)
        token = _prep_dev(client, db, "dev_ca_unreadable")
        resp = _get(client, token, _QUICKSTART_URL)
        assert resp.status_code == 503
        assert "讀不到" in resp.json()["detail"]
    finally:
        ca.chmod(0o644)


def test_unreadable_scaffold_file_is_503_not_500(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    lock = inputs["scaffold"] / "requirements.lock"
    lock.chmod(0)
    try:
        try:
            lock.read_bytes()
        except OSError:
            pass
        else:
            pytest.skip("this uid can still read a mode 000 file")
        _configure(monkeypatch, inputs)
        token = _prep_dev(client, db, "dev_lock_unreadable")
        resp = _get(client, token, _QUICKSTART_URL)
        assert resp.status_code == 503
        assert "讀不到" in resp.json()["detail"]
    finally:
        lock.chmod(0o644)


def test_unparseable_ca_is_503(client, db, monkeypatch, tmp_path):
    """A path that exists is not proof it holds a usable chain."""
    inputs = build_all_inputs(tmp_path)
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    build_profile(inputs["profile"], ca_path=junk)
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_ca_junk")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert "PEM" in resp.json()["detail"]


def test_bound_download_prefills_registered_base_model(client, db, monkeypatch, tmp_path):
    """註冊後下載的包要把 agent 的底層模型名稱寫進 LLM_MODEL。"""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    dev = make_user(db, username="dev_model_gap", role="developer")
    model = make_model(db, name="unmapped-model")
    agent = make_agent(db, owner=dev, name="gap-agent")
    agent.base_model_id = model.id
    db.commit()
    token = login(client, "dev_model_gap")
    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 200, resp.content[:400]
    with _zip_of(resp) as zf:
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert f"ANILA_AGENT_ID={agent.id}\n" in env_text
    assert "LLM_BASE_URL=https://anila.test/v1\n" in env_text
    assert "\nLLM_MODEL=unmapped-model\n" in env_text


@pytest.mark.parametrize(
    "broken, needle",
    [
        ({"csp_base_url": "http://anila.test"}, "https"),
        ({"csp_base_url": "https://anila.test/api"}, "路徑"),
    ],
)
def test_malformed_profile_is_503(client, db, monkeypatch, tmp_path, broken, needle):
    inputs = build_all_inputs(tmp_path)
    base = {
        "csp_base_url": "https://anila.test",
        "csp_ca_file": str(inputs["ca"]),
    }
    base.update(broken)
    inputs["profile"].write_text(json.dumps(base), encoding="utf-8")
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, f"dev_bad_profile_{needle}")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 503
    assert needle in resp.json()["detail"]


def test_profile_with_only_csp_base_url_downloads_generic_package(
    client, db, monkeypatch, tmp_path
):
    """No model section and no agent_id. The release set plus the origin is enough."""
    inputs = build_all_inputs(tmp_path)
    inputs["profile"].write_text(
        json.dumps({"csp_base_url": "https://anila.test"}),
        encoding="utf-8",
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_origin_only")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200, resp.content[:500]
    with _zip_of(resp) as zf:
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert "CSP_BASE_URL=https://anila.test\n" in env_text
    assert "ANILA_CA_FILE=/app/ca.pem\n" in env_text
    assert "LLM_BASE_URL=https://anila.test/v1\n" in env_text
    assert env_text.rstrip().endswith("LLM_AUTH_REQUIRED=true")
    assert "\nANILA_AGENT_ID=\n" in env_text
    assert "\nLLM_MODEL=\n" in env_text
    assert "註冊" in env_text
    assert "模型" in env_text
    assert "LLM_API_KEY" not in env_text


def test_legacy_model_section_is_ignored(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    inputs["profile"].write_text(
        json.dumps(
            {
                "csp_base_url": "https://anila.test",
                "csp_ca_file": str(inputs["ca"]),
                "default_model_key": "nope",
                "models": {"7": {"llm_model": "x"}},
            }
        ),
        encoding="utf-8",
    )
    _configure(monkeypatch, inputs)
    token = _prep_dev(client, db, "dev_ignore_models")
    resp = _get(client, token, _QUICKSTART_URL)
    assert resp.status_code == 200, resp.content[:500]
    with _zip_of(resp) as zf:
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
    assert "LLM_BASE_URL=https://anila.test/v1\n" in env_text
    assert "llm.test" not in env_text
    assert "\nLLM_MODEL=\n" in env_text


# ── advanced example ─────────────────────────────────────────────────────────


def test_advanced_example_download_has_its_own_root_and_name(
    client, db, monkeypatch, tmp_path
):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_advanced", role="developer")
    token = login(client, "dev_advanced")

    resp = _get(client, token, _ADVANCED_URL)
    assert resp.status_code == 200
    final = resp.headers.get("content-disposition", "")
    assert "anila-agent-advanced-example-1.0.0.zip" in final

    with _zip_of(resp) as zf:
        names = set(zf.namelist())
    assert "anila-agent-advanced-example/pyproject.toml" in names
    assert "anila-agent-advanced-example/anila_agent/__init__.py" in names
    # The advanced example must never be silently substituted for quickstart.
    assert not any(n.startswith("anila-agent-quickstart/") for n in names)


def test_advanced_example_excludes_secrets_and_keeps_env_example(
    client, db, monkeypatch, tmp_path
):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_advanced_filter", role="developer")
    token = login(client, "dev_advanced_filter")

    resp = _get(client, token, _ADVANCED_URL)
    assert resp.status_code == 200
    with _zip_of(resp) as zf:
        names = set(zf.namelist())
    assert "anila-agent-advanced-example/.env.example" in names
    assert "anila-agent-advanced-example/.env" not in names
    assert "anila-agent-advanced-example/private.key" not in names
    assert not any(n.endswith(".pyc") for n in names)
    assert b"PRIVATE KEY" not in resp.content


def test_advanced_example_escaping_symlink_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    outside = tmp_path / "adv-outside"
    outside.mkdir()
    (outside / "note.md").write_text("x\n")
    (inputs["advanced"] / "note.md").symlink_to(outside / "note.md")
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_advanced_symlink", role="developer")
    token = login(client, "dev_advanced_symlink")
    resp = _get(client, token, _ADVANCED_URL)
    assert resp.status_code == 503
    assert "symlink" in resp.json()["detail"]


def test_advanced_example_missing_dir_is_503(client, db, monkeypatch, tmp_path):
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    monkeypatch.setattr(
        "app.api.agents.registration._TEMPLATE_DIR", tmp_path / "nope"
    )
    make_user(db, username="dev_advanced_missing", role="developer")
    token = login(client, "dev_advanced_missing")
    resp = _get(client, token, _ADVANCED_URL)
    assert resp.status_code == 503


def test_quickstart_root_does_not_look_like_advanced(
    client, db, monkeypatch, tmp_path
):
    """The old zip root was ``anila-agent/``; both new roots are unambiguous."""
    inputs = build_all_inputs(tmp_path)
    _configure(monkeypatch, inputs)
    make_user(db, username="dev_roots", role="developer")
    token = login(client, "dev_roots")

    with _zip_of(_get(client, token, _QUICKSTART_URL)) as zf:
        quick_names = zf.namelist()
    with _zip_of(_get(client, token, _ADVANCED_URL)) as zf:
        adv_names = zf.namelist()
    assert all(n.startswith("anila-agent-quickstart/") for n in quick_names)
    assert all(n.startswith("anila-agent-advanced-example/") for n in adv_names)
    assert "anila-agent-quickstart/agent.py" in quick_names
    assert "anila-agent-advanced-example/app.py" not in adv_names  # optional


# ── real scaffold tree (the other agent's package) ───────────────────────────

_REAL_SCAFFOLD = (
    Path(__file__).resolve().parents[3] / "packages" / "anila-agent-quickstart"
)


def _real_scaffold_copy(tmp_path: Path) -> Path:
    """Copy the tracked scaffold so a synthesized lock can replace the gap one."""
    import shutil

    dest = tmp_path / "real-scaffold"
    shutil.copytree(
        _REAL_SCAFFOLD,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests", "build"),
    )
    return dest


def _lock_for_requirements_in(scaffold: Path, wheel_dir: Path) -> None:
    """Synthesize a small hash lock that covers the real requirements.in.

    The shipped lock is the real Python 3.13 hash lock. This helper still
    substitutes a tiny one so the download test does not depend on wheel
    files, which §12 no longer puts in the zip.
    """
    from tests.quickstart_fixtures import make_wheel

    names = [
        line.split("==")[0].strip()
        for line in (scaffold / "requirements.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert names, "real requirements.in has no direct dependencies"
    lock_lines = ["# synthesized for the real-scaffold test"]
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        version = f"1.{index}.0"
        _, digest = make_wheel(wheel_dir, name, version)
        lock_lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}")
    (scaffold / "requirements.lock").write_text(
        "\n".join(lock_lines) + "\n", encoding="utf-8"
    )


@pytest.mark.skipif(not _REAL_SCAFFOLD.is_dir(), reason="scaffold not landed yet")
def test_real_scaffold_bundles_and_prefills_without_touching_verifier(
    client, db, monkeypatch, tmp_path
):
    """The downloader must work on the real tree, not only on a synthetic one.

    Pins the two rewrite anchors (``AGENT_NAME`` with annotation-free form and
    ``COLLECTION_ID`` with a ``#`` comment) against the file the other author
    actually wrote — a scaffold refactor that drops a marker would otherwise
    only fail in production.
    """
    inputs = build_all_inputs(tmp_path)
    scaffold = _real_scaffold_copy(tmp_path)
    assert (scaffold / "agent.py").read_text(encoding="utf-8").count("AGENT_NAME") == 1
    shipped_lock = (scaffold / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in shipped_lock
    assert "未驗證" not in shipped_lock
    _lock_for_requirements_in(scaffold, inputs["wheels"])
    inputs["scaffold"] = scaffold

    dev = make_user(db, username="dev_real_scaffold", role="developer")
    model = make_model(db, name="m-real")
    build_profile(inputs["profile"], ca_path=inputs["ca"], model_keys=(str(model.id),))
    canonical = inputs["advanced"] / "verifier-source.py"
    _configure(monkeypatch, inputs)
    agent = make_agent(db, owner=dev, name="real-agent")
    agent.base_model_id = model.id
    db.commit()
    _bind(db, agent, [11])
    token = login(client, "dev_real_scaffold")

    resp = _get(client, token, _QUICKSTART_URL, agent_id=agent.id)
    assert resp.status_code == 200, resp.json()
    with _zip_of(resp) as zf:
        agent_src = zf.read("anila-agent-quickstart/agent.py").decode()
        assert zf.read("anila-agent-quickstart/anila_verify.py") == canonical.read_bytes()
        assert not any("wheelhouse" in name or name.endswith(".whl") for name in zf.namelist())
        manifest = json.loads(zf.read("anila-agent-quickstart/bundle.json"))
        assert manifest["compatible_lab_image_version"] == (
            (scaffold / "IMAGE_VERSION").read_text(encoding="utf-8").strip()
        )
        # The real file keeps its full docstring and comment markers.
        assert 'AGENT_NAME = "real-agent"' in agent_src
        assert "COLLECTION_ID: int | None = 11" in agent_src
        assert "async def respond" in agent_src
        assert "五分鐘路徑不改" in agent_src
        env_text = zf.read("anila-agent-quickstart/deployment.env").decode()
        readme = zf.read("anila-agent-quickstart/README.md").decode()
        import ast

        ast.parse(agent_src)  # prefilled file must still be valid Python
    assert f"ANILA_AGENT_ID={agent.id}\n" in env_text
    assert "\nLLM_MODEL=m-real\n" in env_text
    assert "LLM_BASE_URL=https://anila.test/v1\n" in env_text
    assert "export LLM_API_KEY" in readme
    assert "LLM_API_KEY=" not in env_text


# ── deployment contract (must match infra/compose) ───────────────────────────


def test_mount_paths_and_env_names_match_compose():
    """The knobs ops must supply, pinned so a rename cannot silently 404.

    ``infra/compose/platform.yml`` and ``dev.yml`` mount the scaffold. §12
    removes the wheelhouse mount: wheels are an image-build input, not a
    CSP download input.
    """
    from app.api.agents import _quickstart_bundle as bundle

    assert bundle.ENV_QUICKSTART_DIR == "ANILA_QUICKSTART_DIR"
    assert bundle.ENV_PROFILE_PATH == "ANILA_QUICKSTART_PROFILE"
    assert not hasattr(bundle, "ENV_WHEELHOUSE_DIR")
    assert not hasattr(bundle, "DEFAULT_WHEELHOUSE_DIR")
    assert bundle.DEFAULT_QUICKSTART_DIR == "/app/anila-quickstart"
    assert bundle.DEFAULT_ADVANCED_DIR == "/app/anila-template"

    compose = (
        Path(__file__).resolve().parents[3] / "infra" / "compose" / "platform.yml"
    )
    text = compose.read_text(encoding="utf-8")
    assert "/app/anila-quickstart:ro" in text
    assert "anila-quickstart-wheels" not in text
    assert "/app/anila-template:ro" in text

    dev = compose.parent / "dev.yml"
    dev_text = dev.read_text(encoding="utf-8")
    assert "/app/anila-quickstart:ro" in dev_text
    assert "anila-quickstart-wheels" not in dev_text

    # Directory mount: a missing profile file must not become a host directory
    # at the json path. Compose points the process at a file inside the mount.
    for blob, host_default in (
        (text, "../../share/quickstart"),
        (dev_text, "../../share-dev/quickstart"),
    ):
        assert f"${{ANILA_QUICKSTART_PROFILE_DIR:-{host_default}}}:/app/anila-quickstart-profile:ro" in blob
        assert "ANILA_QUICKSTART_PROFILE: /app/anila-quickstart-profile/profile.json" in blob
        assert not any(
            ".json:" in line and "anila-quickstart-profile" in line
            for line in blob.splitlines()
        )
    ignore = (compose.parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "share/quickstart/" in ignore
    assert "share-dev/quickstart/" in ignore


def test_deployment_env_key_set_is_closed_and_non_secret():
    """A secret in ``deployment.env`` would be the one thing a zip cannot hold."""
    from app.api.agents import _quickstart_bundle as bundle

    assert set(bundle.QUICKSTART_FILES) == {
        "agent.py", "server.py", "platform_io.py", "llm.py",
        "requirements.in", "requirements.lock", "Dockerfile",
        "compose.yaml", ".gitignore", "README.md", "LICENSE",
        "run.sh", "IMAGE_VERSION",
    }
    rendered = bundle.render_deployment_env(
        bundle.SiteProfile("https://x.example", None),
        1,
    ).decode()
    assignments = {
        line.split("=", 1)[0]
        for line in rendered.splitlines()
        if line and not line.startswith("#")
    }
    assert "LLM_API_KEY" not in assignments
    assert assignments == {
        "CSP_BASE_URL",
        "ANILA_CA_FILE",
        "ANILA_AGENT_ID",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_AUTH_REQUIRED",
    }
    assert "LLM_BASE_URL=https://x.example/v1\n" in rendered
    assert "\nLLM_MODEL=\n" in rendered


def test_agent_name_rewrite_refuses_ambiguous_anchor():
    from app.api.agents._quickstart_bundle import BundleError, _rewrite_constant

    assert _rewrite_constant("X = 1\n", "X", "2") == "X = 2\n"
    # '#' inside a string literal is not a comment.
    assert _rewrite_constant('X = "a#b"  # c\n', "X", '"z"') == 'X = "z"  # c\n'
    with pytest.raises(BundleError):
        _rewrite_constant("X = 1\nX = 2\n", "X", "3")
    with pytest.raises(BundleError):
        _rewrite_constant("Y = 1\n", "X", "3")


# ── builder unit tests (kill mutations the route layer would mask) ───────────


def test_resolve_collection_id_refuses_out_of_set_pick_at_builder_level():
    """The route's own 400 must not be the only thing enforcing the bound set.

    Direct builder callers (and a future second route) bypass the endpoint, so
    the assembly step has to refuse an out-of-set id itself — otherwise this
    code is dead and a mutation removing it survives.
    """
    from app.api.agents._quickstart_bundle import BundleError, _AgentInputs, _resolve_collection_id

    assert _resolve_collection_id(_AgentInputs(bound_collection_ids=[9])) == (9, True)
    assert _resolve_collection_id(_AgentInputs(bound_collection_ids=[])) == (None, False)
    assert _resolve_collection_id(_AgentInputs(bound_collection_ids=[1, 2])) == (None, False)
    assert _resolve_collection_id(
        _AgentInputs(bound_collection_ids=[1, 2], requested_collection_id=2)
    ) == (2, True)
    with pytest.raises(BundleError, match="不在 agent 已綁定"):
        _resolve_collection_id(
            _AgentInputs(bound_collection_ids=[1], requested_collection_id=5)
        )
    with pytest.raises(BundleError, match="未綁定任何知識庫"):
        _resolve_collection_id(
            _AgentInputs(bound_collection_ids=[], requested_collection_id=5)
        )


def test_parse_requirements_joins_continuations_and_reports_unparsed():
    from app.api.agents._quickstart_bundle import _parse_requirements

    reqs, unparsed = _parse_requirements(
        "# comment\n"
        "fastapi==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        "httpx>=0.9\n"
        "-r other.txt\n"
    )
    assert reqs == {"fastapi": {"version": "1.2.3", "hashes": {"a" * 64}}}
    # An unpinned direct line is reported, not silently dropped.
    assert unparsed == ["httpx>=0.9"]


def test_lock_coverage_requires_presence_for_bare_names():
    from app.api.agents._quickstart_bundle import (
        BundleError,
        validate_lock_covers_requirements_in,
    )

    lock = "fastapi==1.2.3 \\\n    --hash=sha256:" + "b" * 64 + "\n"
    # Bare name in the .in, present in the lock → satisfied (no version match).
    validate_lock_covers_requirements_in(lock, "fastapi\n")
    # Pinned .in that disagrees with the lock → refused.
    with pytest.raises(BundleError, match="fastapi"):
        validate_lock_covers_requirements_in(lock, "fastapi==9.9.9\n")
    # Missing from the lock entirely → refused.
    with pytest.raises(BundleError, match="uvicorn"):
        validate_lock_covers_requirements_in(lock, "uvicorn\n")


def test_advanced_filter_keeps_env_example_and_drops_local_artifacts(tmp_path):
    """The ``.env`` exclusion is a decision; ``.env.example`` must survive."""
    from app.api.agents._quickstart_bundle import _skip_advanced

    for name in (".env", ".env.example", "private.key", "notes.md"):
        (tmp_path / name).write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("")

    def skip(relative: str) -> bool:
        return _skip_advanced(Path(relative), tmp_path / relative)

    assert skip(".env") is True
    assert skip(".env.example") is False
    assert skip("private.key") is True
    assert skip("__pycache__") is True
    assert skip("nested/.git") is True
    # A real, ordinary file survives — the filter is not rejecting everything.
    assert skip("notes.md") is False
