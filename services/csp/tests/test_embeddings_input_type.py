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


def test_public_surfaces_read_it_rather_than_hardcoding_document():
    """Both embeddings routes must consult the caller, not a literal.

    Not a source grep for the *value*: this asserts the handler bodies call
    the extractor and pass its result, so re-pinning either route back to a
    hardcoded role fails here.
    """
    import inspect

    from app.api import proxy as proxy_api

    for handler in (proxy_api.embeddings_v1, proxy_api.embeddings_v2):
        src = inspect.getsource(handler)
        assert "_pop_input_type(body)" in src, handler.__name__
        assert "embedding_input_role=input_type" in src, handler.__name__
        assert 'embedding_input_role="document"' not in src, handler.__name__
