"""_hydrate_images: resolve image_ref / diagram_dot / image_prompt into image_data.

Phase 4 (2026-05-23) — adapted from csp baseline for anila-studio:
- `upload_dir` positional argument removed; `bearer` keyword required.
- `image_ref` resolution now flows through ``csp_client.fetch_image_blob``
  (HTTP) rather than reading from a shared upload_dir mount. Tests patch
  ``app.services.studio_render.fetch_image_blob`` (where _hydrate_images now
  lives after the god-module split) to inject deterministic bytes.
- ``images_lookup`` shape: ``{image_id_str: {"image_id": int, "mime": str}}``
  (the studio module casts ``meta["image_id"]`` to ``int`` before calling
  ``fetch_image_blob``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api.studio import _hydrate_images


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_BEARER = "test-bearer-token"


@pytest.fixture
def existing_image() -> dict:
    """One pre-existing ingestion image lookup entry keyed by image_id.

    The studio module reads ``meta["image_id"]`` and calls
    ``fetch_image_blob(int(meta["image_id"]), bearer=...)`` — tests mock
    that function instead of writing to disk.
    """
    return {"img-abc": {"image_id": 42, "mime": "image/png"}}


@pytest.fixture
def fetch_blob_mock():
    """Patch ``fetch_image_blob`` to return ``(_PNG, "image/png")``.

    Yields the mock so individual tests can assert on call args if needed.
    """
    with patch(
        "app.services.studio_render.fetch_image_blob",
        new=AsyncMock(return_value=(_PNG, "image/png")),
    ) as m:
        yield m


@pytest.mark.asyncio
async def test_hydrate_image_ref_unchanged_behavior(existing_image, fetch_blob_mock):
    """image_ref path still works — Task 5 must not regress."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG  # unused on this slide

    spec = {"slides": [{"title": "X", "bullets": ["a"], "image_ref": "img-abc"}]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()
    fetch_blob_mock.assert_awaited_once_with(42, bearer=_BEARER)


@pytest.mark.asyncio
async def test_role_set_requests_slide_images_via_csp(monkeypatch, fetch_blob_mock):
    """生圖角色有設定時，配圖打 CSP 的 images 代理，不打模型主機。"""
    import base64

    import httpx
    import respx

    from app.config import settings

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    png_b64 = base64.b64encode(_PNG).decode("ascii")
    spec = {"slides": [
        {"title": "封面", "bullets": ["x"]},
        {
            "title": "Y",
            "bullets": ["b"],
            "image_prompt": "a tank",
            "image_kind": "illustration",
            "layout_kind": "image_focus",
        },
    ]}
    with respx.mock:
        route = respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            return_value=httpx.Response(
                200, json={"data": [{"b64_json": png_b64}]}
            )
        )
        result = await _hydrate_images(
            spec, {}, bearer=_BEARER, default_aspect="16:9",
        )

    assert route.called
    body = route.calls[-1].request
    sent = body.read() if hasattr(body, "read") else body.content
    import json
    payload = json.loads(sent)
    assert payload["model"] == "painter"
    assert "a tank" in payload["prompt"]
    assert str(route.calls[-1].request.url).startswith(settings.CSP_BASE_URL)
    assert "flux" not in str(route.calls[-1].request.url)
    auth = route.calls[-1].request.headers.get("authorization", "")
    assert auth == f"Bearer {_BEARER}"
    slide = result["slides"][1]
    assert slide["image_data"].startswith("data:image/png;base64,")
    assert "image_prompt" not in slide
    assert slide.get("layout_kind") == "image_focus"


@pytest.mark.asyncio
async def test_role_unset_leaves_no_image_frames(fetch_blob_mock):
    """沒設生圖角色時，不留空的配圖框，也不留佔位文字。"""
    spec = {"slides": [
        {
            "title": "封面",
            "bullets": ["重點"],
            "layout_kind": "image_focus",
            "image_prompt": "hero scene",
            "image_kind": "illustration",
        },
        {
            "title": "內文",
            "bullets": ["一", "二"],
            "layout_kind": "image_focus",
            "image_prompt": "a scene",
            "image_kind": "illustration",
        },
    ]}
    result = await _hydrate_images(
        spec, {}, bearer=_BEARER, flux_provider=None, default_aspect="16:9",
    )
    for slide in result["slides"]:
        assert "image_data" not in slide
        assert "image_prompt" not in slide
        assert slide.get("image_kind") != "illustration"
        assert slide.get("layout_kind") != "image_focus"
        text = " ".join(
            str(slide.get(key) or "")
            for key in ("title", "bullets", "speaker_notes")
        )
        assert "placeholder" not in text.lower()
        assert "待補" not in text
        assert "（圖片）" not in text


@pytest.mark.asyncio
async def test_cover_without_image_request_stays_text(fetch_blob_mock):
    """封面沒有 image_prompt 時，不主動生圖。"""
    spec = {"slides": [{"title": "cover", "bullets": ["a"]}]}
    result = await _hydrate_images(
        spec, {}, bearer=_BEARER, default_aspect="16:9",
    )
    assert "image_data" not in result["slides"][0]


@pytest.mark.asyncio
async def test_image_ref_wins_over_image_prompt(existing_image, fetch_blob_mock):
    """If both set, prefer image_ref (existing curated content)."""
    spec = {"slides": [{
        "title": "Z", "bullets": ["c"],
        "image_ref": "img-abc",
        "image_prompt": "should not be called",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, default_aspect="16:9"
    )

    assert result["slides"][0]["image_data"].startswith("data:image/png;base64,")
    assert "image_prompt" not in result["slides"][0]


@pytest.mark.asyncio
async def test_unset_role_drops_image_prompt(existing_image, fetch_blob_mock):
    """沒有生圖角色時，image_prompt 不會留在規格裡。"""
    spec = {"slides": [{
        "title": "W", "bullets": ["d"],
        "image_prompt": "this will fail",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s


@pytest.mark.asyncio
async def test_no_provider_skips_image_prompt(existing_image, fetch_blob_mock):
    """If flux_provider is None (FLUX not configured), prompt path is
    skipped silently — slide falls back to standard layout."""
    spec = {"slides": [{
        "title": "V", "bullets": ["e"],
        "image_prompt": "no provider available",
    }]}
    result = await _hydrate_images(
        spec, existing_image, bearer=_BEARER, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s


@pytest.mark.asyncio
async def test_diagram_dot_renders_via_graphviz(existing_image, fetch_blob_mock):
    """Studio Fix 2: image_kind='diagram' + diagram_dot → inline PNG.

    FLUX must NOT be called for diagram slides — diagrams need crisp
    text labels that FLUX can't produce.
    """
    flux = AsyncMock()  # should remain uncalled

    spec = {"slides": [{
        "title": "Architecture",
        "bullets": ["overview"],
        "image_kind": "diagram",
        "diagram_dot": "digraph G { A -> B }",
    }]}

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec"
    ) as mock_exec:
        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(
            return_value=(_PNG, b"")
        )
        mock_exec.return_value = fake_proc

        result = await _hydrate_images(
            spec, existing_image, bearer=_BEARER,
            flux_provider=flux, default_aspect="16:9",
        )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    # Diagram fields consumed (renderer doesn't see them, image_data won)
    assert "diagram_dot" not in s
    assert "image_kind" not in s
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_diagram_render_failure_drops_dot(existing_image, fetch_blob_mock):
    """When `dot` returns None (syntax error / missing binary / timeout),
    drop diagram_dot + image_kind so renderer falls back to standard.

    image_prompt is intentionally NOT tried as a fallback: the LLM
    declared this a diagram, not an illustration; sending it to FLUX
    would put garbled-text output back on the slide.
    """
    flux = AsyncMock()  # should remain uncalled

    spec = {"slides": [{
        "title": "Broken diagram",
        "bullets": ["x"],
        "image_kind": "diagram",
        "diagram_dot": "digraph { bad syntax",
    }]}

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("dot not installed"),
    ):
        result = await _hydrate_images(
            spec, existing_image, bearer=_BEARER,
            flux_provider=flux, default_aspect="16:9",
        )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "diagram_dot" not in s
    assert "image_kind" not in s
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_image_request_failure_drops_frames_and_keeps_kb_images(
    existing_image, fetch_blob_mock, monkeypatch,
):
    """生圖失敗時不留空框；知識庫裡已有的圖仍嵌進去。"""
    import httpx
    import respx

    from app.config import settings

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    spec = {"slides": [
        {"title": "A", "bullets": ["a"], "image_ref": "img-abc", "layout_kind": "image_focus"},
        {
            "title": "B",
            "bullets": ["b"],
            "image_prompt": "new image",
            "image_kind": "illustration",
            "layout_kind": "image_focus",
        },
    ]}
    with respx.mock:
        respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            return_value=httpx.Response(400, json={"detail": "生圖失敗"})
        )
        result = await _hydrate_images(
            spec, existing_image, bearer=_BEARER, default_aspect="16:9",
        )

    assert result["slides"][0]["image_data"].startswith("data:image/png;base64,")
    failed = result["slides"][1]
    assert "image_data" not in failed
    assert "image_prompt" not in failed
    assert failed.get("layout_kind") != "image_focus"
    assert "待補" not in str(failed.get("bullets"))


def test_outbound_image_prompt_matches_router_filter_and_drops_kb_text():
    from anila_core.api.router_prompts import redact_internal_details

    from app.services.image_prompt import outbound_image_prompt, redact_image_prompt_details

    sample = (
        "wide shot of a ridge at dusk https://csp.internal/x "
        "ANILA_DB_PASSWORD /var/anila/secrets/jwt.py"
    )
    assert redact_image_prompt_details(sample) == redact_internal_details(sample)
    cleaned = outbound_image_prompt(sample)
    assert cleaned is not None
    assert "ridge" in cleaned
    assert "https://" not in cleaned
    assert "ANILA_DB_PASSWORD" not in cleaned
    assert outbound_image_prompt("a tank on a ridge") == "a tank on a ridge"
    leaked = "來源：規章.pdf chunk leaf-00002 第 3 條 " + ("申訴期限內提出。" * 20)
    assert outbound_image_prompt(leaked) is None
    assert outbound_image_prompt("a calm ridge " * 40) is None


@pytest.mark.asyncio
async def test_raw_kb_prompt_is_not_sent(monkeypatch):
    import httpx
    import respx

    from app.config import settings

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    spec = {"slides": [{
        "title": "內文",
        "bullets": ["一"],
        "image_prompt": "來源：規章.pdf 第 3 條 " + ("機密內文" * 30),
        "image_kind": "illustration",
        "layout_kind": "image_focus",
    }]}
    with respx.mock:
        route = respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": "aaaa"}]})
        )
        result = await _hydrate_images(spec, {}, bearer=_BEARER, default_aspect="16:9")
    assert not route.called
    assert "image_data" not in result["slides"][0]


@pytest.mark.asyncio
async def test_image_request_carries_task_id_and_redacts_prompt(monkeypatch):
    import base64
    import json

    import httpx
    import respx

    from app.config import settings

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    png_b64 = base64.b64encode(_PNG).decode("ascii")
    spec = {"slides": [{
        "title": "內文",
        "bullets": ["一"],
        "image_prompt": "wide shot of a ridge at dusk https://csp.internal/secret",
        "image_kind": "illustration",
        "layout_kind": "image_focus",
    }]}
    with respx.mock:
        route = respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": png_b64}]})
        )
        await _hydrate_images(
            spec, {}, bearer=_BEARER, default_aspect="16:9", task_id="42",
        )
    assert route.called
    sent = json.loads(route.calls[-1].request.content)
    assert "https://" not in sent["prompt"]
    assert "ridge" in sent["prompt"]
    assert route.calls[-1].request.headers["x-anila-task-id"] == "42"


@pytest.mark.asyncio
async def test_generated_image_keeps_jpeg_mime_and_drops_garbage(monkeypatch):
    import base64

    import httpx
    import respx

    from app.config import settings

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    jpeg_b64 = base64.b64encode(jpeg).decode("ascii")
    spec = {"slides": [
        {
            "title": "封面",
            "bullets": ["x"],
            "image_prompt": "a ridge at dusk",
            "image_kind": "illustration",
        },
        {
            "title": "內文",
            "bullets": ["y"],
            "image_prompt": "a calm harbor",
            "image_kind": "illustration",
        },
    ]}
    with respx.mock:
        route = respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            side_effect=[
                httpx.Response(200, json={"data": [{"b64_json": jpeg_b64}]}),
                httpx.Response(
                    200,
                    json={"data": [{"b64_json": base64.b64encode(b"NOT-AN-IMAGE").decode()}]},
                ),
            ]
        )
        result = await _hydrate_images(spec, {}, bearer=_BEARER, default_aspect="16:9")
    assert route.call_count == 2
    assert result["slides"][0]["image_data"].startswith("data:image/jpeg;base64,")
    assert "image_data" not in result["slides"][1]


@pytest.mark.asyncio
async def test_decoded_image_over_the_cap_is_dropped(monkeypatch):
    import base64

    import httpx
    import respx

    from app.clients import csp_client
    from app.config import settings

    monkeypatch.setattr(csp_client, "MAX_DECODED_IMAGE_BYTES", 16, raising=False)

    async def _ready() -> str:
        return "painter"

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _ready,
        raising=False,
    )
    png_b64 = base64.b64encode(_PNG).decode("ascii")
    spec = {"slides": [{
        "title": "封面",
        "bullets": ["x"],
        "image_prompt": "a ridge at dusk",
        "image_kind": "illustration",
    }]}
    with respx.mock:
        respx.post(f"{settings.CSP_BASE_URL}/v1/images/generations").mock(
            return_value=httpx.Response(200, json={"data": [{"b64_json": png_b64}]})
        )
        result = await _hydrate_images(spec, {}, bearer=_BEARER, default_aspect="16:9")
    assert "image_data" not in result["slides"][0]
