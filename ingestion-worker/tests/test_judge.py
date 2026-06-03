"""Tests for ingestion_worker.judge — LLM-as-judge scoring.

Covers (per assignment):
- JudgeCredential.__repr__ masks api_key (no plaintext Bearer token leak).
- _SCORE_RE parsing exercised via score_one (digit -> int 1/2/3).
- score_one happy path with respx-mocked POST /chat/completions.
- empty chunk_contents -> None (no HTTP issued).
- SSRF guard: an endpoint_url rejected by validate_outbound_url -> None,
  and the outbound POST is NEVER issued.
- HTTP error (non-2xx + transport error) -> None.
- unparseable content -> None.
- bad response shape -> None.

SKIPPED: load_judge_credential — needs a real anila_core PgPool + DB row
(asyncpg connection + user_llm_credentials table + AES ciphertext). Out of
scope for a pure-unit suite; covered by integration tests with a live DB.

HTTP is mocked with respx; no network is hit. The SSRF endpoint
(https://localhost/) is rejected by the deny-list regardless of the
ANILA_ALLOW_HTTP_ENDPOINT / ANILA_ALLOW_PRIVATE_ENDPOINT env flags, so the
guard tests stay deterministic across environments.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ingestion_worker.judge import JudgeCredential, _SCORE_RE, score_one


# A public-looking https endpoint that passes validate_outbound_url.
# httpx joins base_url + "/chat/completions" -> ".../chat/completions".
_GOOD_ENDPOINT = "https://api.example.com"
_COMPLETIONS_URL = "https://api.example.com/chat/completions"

# Rejected by the SSRF deny-list (host == "localhost") even with https
# scheme, independent of any opt-in env flag.
_BAD_ENDPOINT = "https://localhost/"


def _cred(endpoint_url: str = _GOOD_ENDPOINT) -> JudgeCredential:
    return JudgeCredential(
        endpoint_url=endpoint_url,
        model_name="gpt-4o-mini",
        api_key="sk-super-secret-bearer-token",
    )


def _completion(content: str) -> dict:
    """A well-shaped OpenAI-style chat completion payload."""
    return {"choices": [{"message": {"content": content}}]}


# --------------------------------------------------------------------------
# JudgeCredential.__repr__ masking
# --------------------------------------------------------------------------

def test_repr_masks_api_key():
    cred = _cred()
    text = repr(cred)
    assert "sk-super-secret-bearer-token" not in text
    assert "api_key='***'" in text
    # Non-secret fields are still visible for debugging.
    assert "api.example.com" in text
    assert "gpt-4o-mini" in text


def test_repr_does_not_leak_via_str_or_format():
    cred = _cred()
    # str() falls back to __repr__ for dataclasses; f-string uses it too.
    assert "sk-super-secret-bearer-token" not in str(cred)
    assert "sk-super-secret-bearer-token" not in f"{cred}"


def test_frozen_credential_blocks_mutation():
    cred = _cred()
    with pytest.raises((AttributeError, Exception)):
        cred.api_key = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------
# _SCORE_RE parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        (" 3", "3"),          # leading whitespace tolerated
        ("\n2\n", "2"),       # leading newline tolerated
        ("3 分", "3"),        # digit followed by a word boundary
        ("1 (irrelevant)", "1"),
    ],
)
def test_score_re_matches_valid(text, expected):
    m = _SCORE_RE.match(text)
    assert m is not None
    assert m.group(1) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0",           # out of 1-3 range
        "4",           # out of range
        "31",          # 3 then a digit -> no \b word boundary, rejected
        "score is 2",  # digit not at the start
        "abc",
        "  ",
    ],
)
def test_score_re_rejects_invalid(text):
    assert _SCORE_RE.match(text) is None


# --------------------------------------------------------------------------
# score_one happy path (digit -> int)
# --------------------------------------------------------------------------

@respx.mock
@pytest.mark.parametrize("digit", ["1", "2", "3"])
async def test_score_one_returns_int(digit):
    route = respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=_completion(digit))
    )
    result = await score_one(_cred(), "what is x?", ["chunk a", "chunk b"])
    assert result == int(digit)
    assert isinstance(result, int)
    assert route.called


@respx.mock
async def test_score_one_parses_digit_with_trailing_text():
    """Judge sometimes adds stray text; leading digit still wins."""
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=_completion("2 分，部分相關"))
    )
    result = await score_one(_cred(), "q", ["c"])
    assert result == 2


@respx.mock
async def test_score_one_sends_bearer_and_body():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=_completion("3"))

    respx.post(_COMPLETIONS_URL).mock(side_effect=_handler)
    result = await score_one(_cred(), "my query", ["only chunk"])
    assert result == 3
    assert captured["auth"] == "Bearer sk-super-secret-bearer-token"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["temperature"] == 0.0
    # The single chunk content makes it into the prompt.
    assert "only chunk" in captured["body"]["messages"][0]["content"]


# --------------------------------------------------------------------------
# empty chunk_contents -> None (no HTTP issued)
# --------------------------------------------------------------------------

@respx.mock
async def test_empty_chunks_returns_none_without_request():
    route = respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=_completion("3"))
    )
    result = await score_one(_cred(), "q", [])
    assert result is None
    assert not route.called


# --------------------------------------------------------------------------
# SSRF guard -> None, and request is never issued
# --------------------------------------------------------------------------

@respx.mock
async def test_ssrf_guard_blocks_localhost_no_request():
    # respx with assert_all_mocked default would raise on an unexpected
    # request; we additionally route the (would-be) URL to a sentinel so a
    # leak is unambiguous, then assert it was never touched.
    leak_route = respx.post("https://localhost/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion("3"))
    )
    result = await score_one(_cred(_BAD_ENDPOINT), "q", ["chunk"])
    assert result is None
    assert not leak_route.called


@respx.mock
async def test_ssrf_guard_blocks_metadata_ip():
    leak_route = respx.post("https://169.254.169.254/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion("3"))
    )
    result = await score_one(
        _cred("https://169.254.169.254/"), "q", ["chunk"]
    )
    assert result is None
    assert not leak_route.called


# --------------------------------------------------------------------------
# HTTP error -> None
# --------------------------------------------------------------------------

@respx.mock
async def test_http_status_error_returns_none():
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    result = await score_one(_cred(), "q", ["chunk"])
    assert result is None


@respx.mock
async def test_http_transport_error_returns_none():
    respx.post(_COMPLETIONS_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await score_one(_cred(), "q", ["chunk"])
    assert result is None


# --------------------------------------------------------------------------
# unparseable content -> None
# --------------------------------------------------------------------------

@respx.mock
@pytest.mark.parametrize("content", ["", "no digit here", "score: high", "0", "9"])
async def test_unparseable_content_returns_none(content):
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=_completion(content))
    )
    result = await score_one(_cred(), "q", ["chunk"])
    assert result is None


@respx.mock
async def test_null_content_returns_none():
    """A well-shaped payload whose content is JSON null -> None.

    Regression test for the text[:40]-on-None crash: the unparseable-text
    log slice now guards with ``(text or "")[:40]``, so a null content
    degrades to the soft-failure None path instead of raising TypeError.
    """
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=_completion(None))
    )
    result = await score_one(_cred(), "q", ["chunk"])
    assert result is None


# --------------------------------------------------------------------------
# bad response shape -> None
# --------------------------------------------------------------------------

@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {},                                   # no "choices" -> KeyError
        {"choices": []},                      # empty list -> IndexError
        {"choices": [{}]},                    # no "message" -> KeyError
        {"choices": [{"message": {}}]},       # no "content" -> KeyError
        {"choices": "not-a-list"},            # wrong type -> TypeError/KeyError
    ],
)
async def test_bad_response_shape_returns_none(payload):
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await score_one(_cred(), "q", ["chunk"])
    assert result is None
