"""Unit tests for pure / easily-mockable helpers in ``ingestion_worker.handlers``.

Scope (deliberately narrow — no DB/redis/arq infra stood up):
  * ``_clean_caption``        — pure string transform (reasoning-preamble strip,
                                 paragraph selection, whitespace collapse, truncation).
  * ``_get_vision_provider``  — config-gated lazy factory (the two "return None"
                                 fast paths; we never construct a real VisionProvider).
  * ``_caption_images_into``  — async placeholder rewrite, exercised with the vision
                                 provider monkeypatched to a fake (no network).
  * ``_persist_images``       — only the no-images / mkdir-fail early returns, which
                                 need no DB or embedder.

``_is_uniform_color`` is intentionally NOT tested here — tests/test_uniform_color.py
already owns it.

The narrow terminal-state branches of ``ingest_document`` are covered with
mocked DB helpers.  The full insert path of ``_persist_images`` still requires
a live PgPool / asyncpg connection + an Embedder HTTP endpoint.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from anila_core.ingestion.errors import EmbedError
from ingestion_worker import handlers
from ingestion_worker.handlers import _CAPTION_MAX_CHARS, _clean_caption
from ingestion_worker.settings import settings


# ── fixtures / helpers ───────────────────────────────────────────────────────


class _FakeRef:
    """Minimal stand-in for the parser's ImageRef (only the attrs the
    handler touches: image_bytes / mime / caption)."""

    def __init__(self, image_bytes: bytes, mime: str = "image/png") -> None:
        self.image_bytes = image_bytes
        self.mime = mime
        self.caption = ""


class _FakeVision:
    """Vision provider whose ``describe_image`` returns a canned caption
    without any network call."""

    def __init__(self, caption: str = "A bar chart of quarterly sales.") -> None:
        self._caption = caption
        self.calls: list[tuple[bytes, str]] = []

    async def describe_image(self, image_bytes: bytes, mime: str = "image/png") -> str:
        self.calls.append((image_bytes, mime))
        return self._caption


def _solid_png(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png(size: tuple[int, int] = (64, 64)) -> bytes:
    """A PNG with real variance so ``_is_uniform_color`` does NOT filter it."""
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def reset_vision_cache():
    """Ensure the module-level lazy cache + relevant settings are restored
    after a test mutates them, so tests stay isolated."""
    prev_provider = handlers._vision_provider
    prev_enable = settings.enable_image_captions
    prev_url = settings.vision_url
    try:
        yield
    finally:
        handlers._vision_provider = prev_provider
        settings.enable_image_captions = prev_enable
        settings.vision_url = prev_url


# ── _clean_caption ───────────────────────────────────────────────────────────


def test_clean_caption_empty_input_returns_empty():
    assert _clean_caption("") == ""


def test_clean_caption_passthrough_simple_sentence():
    assert _clean_caption("A red bar chart.") == "A red bar chart."


def test_clean_caption_strips_think_block():
    raw = "<think>internal reasoning here</think>A bar chart showing sales."
    assert _clean_caption(raw) == "A bar chart showing sales."


def test_clean_caption_strips_thinking_block_case_insensitive():
    raw = "<THINKING>noise</THINKING>The actual caption text."
    assert _clean_caption(raw) == "The actual caption text."


def test_clean_caption_strips_thought_preamble_bullets():
    raw = "thought\n* first guess\n* second guess\n\nThe final answer is a pie chart."
    assert _clean_caption(raw) == "The final answer is a pie chart."


def test_clean_caption_picks_last_meaningful_paragraph():
    raw = (
        "Summary header.\n\n"
        "This is the detailed last paragraph describing the chart."
    )
    assert _clean_caption(raw) == "This is the detailed last paragraph describing the chart."


def test_clean_caption_collapses_whitespace_and_strips_leading_markers():
    assert _clean_caption("   *   multiple   spaces   here   ") == "multiple spaces here"


def test_clean_caption_truncates_overlong_to_max_plus_ellipsis():
    out = _clean_caption("a" * 700)
    # Body is truncated to _CAPTION_MAX_CHARS and an ellipsis is appended.
    assert out.endswith("…")
    assert len(out) == _CAPTION_MAX_CHARS + 1
    assert out[:-1] == "a" * _CAPTION_MAX_CHARS


def test_clean_caption_keeps_bullets_when_thats_all_there_is():
    # No non-bullet paragraph >= 20 chars, so the last (bullet) paragraph is
    # kept but leading markers are stripped + whitespace collapsed.
    out = _clean_caption("* short one")
    assert out == "short one"


# ── _get_vision_provider ──────────────────────────────────────────────────────


def test_get_vision_provider_none_when_disabled(reset_vision_cache):
    handlers._vision_provider = None
    settings.enable_image_captions = False
    settings.vision_url = "https://vlm.example.test"
    assert handlers._get_vision_provider() is None


def test_get_vision_provider_none_when_url_empty(reset_vision_cache):
    handlers._vision_provider = None
    settings.enable_image_captions = True
    settings.vision_url = ""
    assert handlers._get_vision_provider() is None


def test_get_vision_provider_returns_cached_instance(reset_vision_cache):
    # When a provider is already cached, the factory returns it without
    # re-constructing (and without touching the import of VisionProvider).
    sentinel = object()
    handlers._vision_provider = sentinel
    settings.enable_image_captions = True
    settings.vision_url = "https://vlm.example.test"
    assert handlers._get_vision_provider() is sentinel


# ── _caption_images_into ──────────────────────────────────────────────────────


async def test_caption_into_no_images_returns_text_unchanged():
    text = "some prose with no placeholder"
    assert await handlers._caption_images_into(text, {}) == text


async def test_caption_into_no_placeholder_returns_text_unchanged():
    images = {"a": _FakeRef(_gradient_png())}
    text = "prose without any IMAGE token"
    assert await handlers._caption_images_into(text, images) == text


async def test_caption_into_provider_off_keeps_placeholder(monkeypatch):
    # _get_vision_provider returns None -> the placeholder is left intact for
    # the chunker's downstream IMAGE-leaf fallback.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: None)
    images = {"img1": _FakeRef(_gradient_png())}
    text = "before [[IMAGE:img1]] after"
    assert await handlers._caption_images_into(text, images) == text


async def test_caption_into_replaces_placeholder_with_caption(monkeypatch):
    vision = _FakeVision("A bar chart of quarterly sales.")
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: vision)
    ref = _FakeRef(_gradient_png())
    images = {"img1": ref}
    out = await handlers._caption_images_into("before [[IMAGE:img1]] after", images)
    assert out == "before [圖片描述：A bar chart of quarterly sales.] after"
    # The cleaned caption is stashed back on the ref for downstream persistence.
    assert ref.caption == "A bar chart of quarterly sales."
    assert len(vision.calls) == 1


async def test_caption_into_uniform_image_skipped_keeps_placeholder(monkeypatch):
    # Uniform-color image (PDF background fill) -> no VLM call, empty caption,
    # placeholder is preserved verbatim.
    vision = _FakeVision()
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: vision)
    ref = _FakeRef(_solid_png((26, 54, 93)))
    images = {"u": ref}
    out = await handlers._caption_images_into("x [[IMAGE:u]] y", images)
    assert out == "x [[IMAGE:u]] y"
    assert vision.calls == []  # short-circuited before the VLM call
    assert ref.caption == ""


async def test_caption_into_oversized_image_skipped(monkeypatch):
    vision = _FakeVision()
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: vision)
    # Force the size limit below our image size so the oversize branch fires.
    monkeypatch.setattr(settings, "vision_max_image_bytes", 1)
    ref = _FakeRef(_gradient_png())
    images = {"big": ref}
    out = await handlers._caption_images_into("a [[IMAGE:big]] b", images)
    assert out == "a [[IMAGE:big]] b"
    assert vision.calls == []


async def test_caption_into_vlm_failure_keeps_placeholder(monkeypatch):
    class _BoomVision:
        async def describe_image(self, image_bytes, mime="image/png"):
            raise RuntimeError("vlm down")

    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: _BoomVision())
    images = {"img1": _FakeRef(_gradient_png())}
    text = "p [[IMAGE:img1]] q"
    # A single failed caption must NOT raise — the placeholder survives.
    assert await handlers._caption_images_into(text, images) == text


async def test_caption_into_malformed_placeholder_left_as_is(monkeypatch):
    # Unterminated "[[IMAGE:" (no closing "]]") -> scanning stops, tail kept,
    # no infinite loop.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: _FakeVision())
    images = {"img1": _FakeRef(_gradient_png())}
    text = "head [[IMAGE:img1 tail-with-no-close"
    assert await handlers._caption_images_into(text, images) == text


async def test_caption_into_unknown_id_keeps_placeholder(monkeypatch):
    # Placeholder id not present in the images dict -> captions.get returns "",
    # so the original token shape is preserved.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda: _FakeVision())
    images = {"known": _FakeRef(_gradient_png())}
    text = "a [[IMAGE:unknown]] b"
    out = await handlers._caption_images_into(text, images)
    assert out == "a [[IMAGE:unknown]] b"


# ── _persist_images (infra-free early returns only) ───────────────────────────


async def test_persist_images_no_images_returns_zero():
    # Empty dict short-circuits before any pool / fs access.
    assert await handlers._persist_images(None, 1, 1, {}, None, None) == 0


async def test_persist_images_mkdir_failure_returns_zero(monkeypatch):
    # If the images dir can't be created, persistence bails with 0 and never
    # touches the (None) pool — exercises the OSError guard.
    def _boom(*_a, **_k):
        raise OSError("read-only fs")

    monkeypatch.setattr(handlers.os, "makedirs", _boom)
    images = {"img1": _FakeRef(_gradient_png())}
    assert await handlers._persist_images(None, 1, 42, images, None, None) == 0


# ── ingest_document terminal convergence ─────────────────────────────────────


def _install_ingest_fakes(monkeypatch, tmp_path, *, chunks, embedder):
    blob_path = tmp_path / "document.txt"
    blob_path.write_text("content", encoding="utf-8")
    document_updates: list[tuple[str, dict]] = []
    job_updates: list[dict] = []
    failures = []

    async def load_meta(_pool, _document_id):
        return {
            "collection_id": 7,
            "storage_path": str(blob_path),
            "uploaded_by": 11,
            "owner_user_id": 12,
            "filename": "document.txt",
            "mime_type": "text/plain",
            "chunking_config": {"strategy": "test", "params": {}},
        }

    async def update_document(_pool, _document_id, status, **kwargs):
        document_updates.append((status, kwargs))

    async def update_job(_pool, _job_id, **kwargs):
        job_updates.append(kwargs)

    async def record_failure(_pool, _job_id, error):
        failures.append(error)

    chunker = SimpleNamespace(requires_embedder=False, chunk=lambda *_args: chunks)
    monkeypatch.setattr(handlers, "_load_document_meta", load_meta)
    monkeypatch.setattr(handlers, "_update_document_status", update_document)
    monkeypatch.setattr(handlers, "_update_job", update_job)
    monkeypatch.setattr(handlers, "_record_job_failure", record_failure)
    monkeypatch.setattr(
        handlers,
        "extract_text",
        lambda *_args: ("parsed content", {}, {}),
    )
    monkeypatch.setattr(handlers, "get_chunker", lambda _strategy: chunker)
    return {
        "ctx": {"pool": object(), "embedder": embedder, "job_id": "job-1"},
        "document_updates": document_updates,
        "job_updates": job_updates,
        "failures": failures,
    }


async def test_ingest_empty_chunks_writes_one_succeeded_terminal_job_state(
    monkeypatch, tmp_path
):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )

    result = await handlers.ingest_document(state["ctx"], document_id=41)

    terminal = [
        update
        for update in state["job_updates"]
        if update.get("status") in {"succeeded", "failed", "cancelled"}
    ]
    assert terminal == [
        {
            "status": "succeeded",
            "succeeded": True,
            "progress_pct": 100,
            "progress_message": "0 chunks indexed",
        }
    ]
    assert state["document_updates"][-1] == (
        "indexed",
        {"chunk_count": 0, "error_message": None},
    )
    assert result == {"chunk_count": 0, "warning": "no chunks produced"}


async def test_ingest_embedding_timeout_converges_document_and_job_to_failed(
    monkeypatch, tmp_path
):
    class TimeoutEmbedder:
        async def embed(self, _texts, *, user_id=None):
            raise EmbedError.timeout("embedding timed out")

    chunk = SimpleNamespace(content="leaf", metadata={})
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[chunk],
        embedder=TimeoutEmbedder(),
    )

    with pytest.raises(EmbedError):
        await handlers.ingest_document(state["ctx"], document_id=42)

    assert state["document_updates"][-1] == (
        "failed",
        {"error_message": "embedding timed out"},
    )
    assert len(state["failures"]) == 1
    assert state["failures"][0].code == "E_EMBED_TIMEOUT"


async def test_ingest_cancellation_converges_document_and_job_then_reraises(
    monkeypatch, tmp_path
):
    class CancelledEmbedder:
        async def embed(self, _texts, *, user_id=None):
            raise asyncio.CancelledError

    chunk = SimpleNamespace(content="leaf", metadata={})
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[chunk],
        embedder=CancelledEmbedder(),
    )

    with pytest.raises(asyncio.CancelledError):
        await handlers.ingest_document(state["ctx"], document_id=43)

    assert state["document_updates"][-1] == (
        "failed",
        {"error_message": "處理已取消或逾時，可重新處理。"},
    )
    terminal = [
        update
        for update in state["job_updates"]
        if update.get("status") in {"succeeded", "failed", "cancelled"}
    ]
    assert terminal == [
        {
            "status": "cancelled",
            "succeeded": True,
            "progress_pct": 100,
            "progress_message": "cancelled or timed out",
        }
    ]
