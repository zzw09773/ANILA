"""``input_type`` on the public embeddings surfaces — the out-of-process door.

``/v1/embeddings`` and ``/v2/embeddings`` used to hardcode
``embedding_input_role="document"``, so an out-of-process caller had no way to
say which side of a Triton embedder's query/documents split it was on. The
platform's own agent SDK goes through exactly that door, so against a Triton
embedder every agent RAG query was embedded as a document: ranking degrades,
nothing errors.

Contract:
- absent / empty → ``document`` (what every existing caller means; silence
  must not change today's behaviour)
- ``"query"`` / ``"document"`` → that side
- anything else → 400, not a silent fallback to document
- the key never reaches the upstream request body
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.proxy import _pop_input_type
from app.models.model_registry import ModelRegistry
from app.services.auth_service import create_tokens

from tests.conftest import make_user


def test_absent_defaults_to_document():
    body = {"model": "m", "input": ["a"]}
    assert _pop_input_type(body) == "document"


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_defaults_to_document(empty):
    assert _pop_input_type({"input": ["a"], "input_type": empty}) == "document"


@pytest.mark.parametrize("value", ["query", "document"])
def test_explicit_values_pass_through(value):
    assert _pop_input_type({"input": ["a"], "input_type": value}) == value


@pytest.mark.parametrize(
    "bogus", ["Query", "QUERY", "documents", "doc", "passage", 1, True, []]
)
def test_unknown_value_is_rejected_not_silently_defaulted(bogus):
    """A typo must fail loudly.

    Falling back to ``document`` on an unrecognised value is how a caller ends
    up believing it opted onto the query side while being embedded as a
    document — the exact silent failure this parameter exists to prevent.
    """
    with pytest.raises(HTTPException) as exc:
        _pop_input_type({"input": ["a"], "input_type": bogus})
    assert exc.value.status_code == 400


def test_key_is_removed_from_the_forwarded_body():
    """OpenAI-compatible upstreams reject unknown fields; the key stops here."""
    body = {"model": "m", "input": ["a"], "input_type": "query"}
    _pop_input_type(body)
    assert "input_type" not in body
    assert body == {"model": "m", "input": ["a"]}


def test_body_without_the_key_is_left_alone():
    body = {"model": "m", "input": ["a"]}
    _pop_input_type(body)
    assert body == {"model": "m", "input": ["a"]}


# ── the HTTP door itself ─────────────────────────────────────────────────────
#
# Everything above tests the extractor in isolation. What this feature promises
# is one level up: what a caller PUTS IN THE BODY decides which Triton tensor
# the request lands on. That link used to be checked by an
# ``inspect.getsource()`` grep, which a mutation walks straight past — adding
# ``input_type = "document"`` after the extractor call on both routes leaves
# every identifier the grep looked for in place, keeps the whole suite green,
# and disables I4 completely. These tests post real HTTP and read the role the
# Triton client is actually asked for; ``test_triton_grpc_wire.py`` carries the
# other half of the chain (role → input tensor name, on real gRPC).


def _register_triton_embedder(db, name="nv-embed-v2"):
    """A triton_grpc embedder at a private IP — no packet is ever sent."""
    model = ModelRegistry(
        name=name,
        display_name=name,
        model_type="embedding",
        endpoint_url="grpc://172.16.120.35:9001",
        api_version="v1",
        protocol="triton_grpc",
        is_active=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@pytest.fixture
def _grpc_endpoint_allowed(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")


def _post_embeddings(client, db, path, body, username="input_type_caller"):
    user = make_user(db, username=username, role="admin")
    token = create_tokens(user)["access_token"]
    return client.post(
        path, json=body, headers={"Authorization": f"Bearer {token}"}
    )


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v2/embeddings"])
@pytest.mark.parametrize(
    "declared,expected_role",
    [("query", "query"), ("document", "document"), (None, "document")],
)
def test_body_input_type_decides_the_triton_role(
    client, db, monkeypatch, _grpc_endpoint_allowed, path, declared, expected_role
):
    """The acceptance invariant: the body decides, not a literal in the route.

    Kills ``input_type = "document"`` inserted after ``_pop_input_type`` on
    either route — the mutation the old source grep could not see.
    """
    _register_triton_embedder(db)

    seen: dict = {}

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        seen["role"] = role
        seen["texts"] = texts
        return [[0.1] * 4]

    monkeypatch.setattr("app.services.triton_grpc.embed_texts", fake_embed)

    body = {"model": "nv-embed-v2", "input": "找出去年的採購紀錄"}
    if declared is not None:
        body["input_type"] = declared

    resp = _post_embeddings(client, db, path, body)

    assert resp.status_code == 200, resp.text
    # The SPA catch-all answers 200 text/html; a JSON content-type is what
    # proves this route ran at all.
    assert resp.headers["content-type"].startswith("application/json")
    assert seen["role"] == expected_role
    assert seen["texts"] == ["找出去年的採購紀錄"]


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v2/embeddings"])
def test_query_and_document_do_not_take_the_same_route_branch(
    client, db, monkeypatch, _grpc_endpoint_allowed, path
):
    """Same text, two callers, two roles — the split is caller-visible.

    A route pinned to one side could still pass one of the cases above; this
    one fails for either pin.
    """
    _register_triton_embedder(db)
    roles: list[str] = []

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        roles.append(role)
        return [[0.1] * 4]

    monkeypatch.setattr("app.services.triton_grpc.embed_texts", fake_embed)

    for i, declared in enumerate(("query", "document")):
        resp = _post_embeddings(
            client,
            db,
            path,
            {"model": "nv-embed-v2", "input": "同一段文字", "input_type": declared},
            username=f"split_caller_{i}",
        )
        assert resp.status_code == 200, resp.text

    assert roles == ["query", "document"]


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v2/embeddings"])
def test_unknown_input_type_is_400_at_the_http_boundary(
    client, db, monkeypatch, _grpc_endpoint_allowed, path
):
    """A typo must never reach the embedder as a silently-defaulted document."""
    _register_triton_embedder(db)
    called: list = []

    def fake_embed(endpoint_url, model_name, texts, *, role, timeout_s=30.0):
        called.append(role)
        return [[0.1] * 4]

    monkeypatch.setattr("app.services.triton_grpc.embed_texts", fake_embed)

    resp = _post_embeddings(
        client,
        db,
        path,
        {"model": "nv-embed-v2", "input": "q", "input_type": "passage"},
    )

    assert resp.status_code == 400, resp.text
    assert called == []
