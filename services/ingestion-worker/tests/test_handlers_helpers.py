"""Unit tests for pure / easily-mockable helpers in ``ingestion_worker.handlers``.

Scope (deliberately narrow — no DB/redis/arq infra stood up):
  * ``_clean_caption``        — pure string transform (reasoning-preamble strip,
                                 paragraph selection, whitespace collapse, truncation).
  * ``_get_vision_provider``  — config-gated lazy factory (the two "return None"
                                 fast paths; we never construct a real VisionProvider).
  * ``_caption_images_into``  — async placeholder rewrite, exercised with the vision
                                 provider monkeypatched to a fake (no network).
  * ``_persist_images``       — the no-images / mkdir-fail early returns (no DB),
                                 plus the re-import embedding invariant (Postgres
                                 only; skipped unless ``ANILA_TEST_PG_DSN``).
  * ``_attach_image_pks_to_chunks`` — stamps ``ingestion_images.id`` onto chunk
                                 metadata without touching embed text.

``_is_uniform_color`` is intentionally NOT tested here — tests/test_uniform_color.py
already owns it.

The ``ingest_document`` job pipeline is still out of scope. The insert /
``ON CONFLICT`` path of ``_persist_images`` covers two directions of the
same invariant: a dead embedder must not wipe an existing embedding, and
a live embedder must replace it. Engine behaviour (Postgres
``ON CONFLICT`` / ``halfvec``) is skipped unless ``ANILA_TEST_PG_DSN``;
SQLite cannot stand in for it. The no-DB half pins COALESCE argument
order so ``COALESCE(existing, EXCLUDED)`` cannot pass as "keeps".
"""
from __future__ import annotations

import io
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager

import pytest
from PIL import Image

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
        self.caption_source_model = None


class _FakeVision:
    """Vision provider whose ``describe_image`` returns a canned caption
    without any network call."""

    def __init__(self, caption: str = "A bar chart of quarterly sales.") -> None:
        self._caption = caption
        self.model = "fake-vision"
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
    prev_providers = dict(handlers._vision_providers)
    prev_enable = settings.enable_image_captions
    prev_url = settings.vision_url
    try:
        yield
    finally:
        handlers._vision_provider = prev_provider
        handlers._vision_providers = prev_providers
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


def test_resolve_caption_intent_null_follows_platform(reset_vision_cache):
    settings.enable_image_captions = False
    settings.vision_model = "gemma4"
    want, model = handlers._resolve_caption_intent(None, None)
    assert want is False
    assert model == "gemma4"
    settings.enable_image_captions = True
    want, model = handlers._resolve_caption_intent(None, None)
    assert want is True


def test_resolve_caption_intent_collection_overrides_platform(reset_vision_cache):
    settings.enable_image_captions = True
    settings.vision_model = "gemma4"
    want, model = handlers._resolve_caption_intent(False, "other-vlm")
    assert want is False
    assert model == "other-vlm"
    settings.enable_image_captions = False
    want, _model = handlers._resolve_caption_intent(True, None)
    assert want is True


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
    out, stats = await handlers._caption_images_into(text, {})
    assert out == text
    assert stats["image_count"] == 0
    assert stats["succeeded"] == 0


async def test_caption_into_no_placeholder_returns_text_unchanged():
    images = {"a": _FakeRef(_gradient_png())}
    text = "prose without any IMAGE token"
    out, stats = await handlers._caption_images_into(text, images)
    assert out == text


async def test_caption_into_provider_off_keeps_placeholder(monkeypatch):
    # _get_vision_provider returns None -> the placeholder is left intact for
    # the chunker's downstream IMAGE-leaf fallback.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: None)
    images = {"img1": _FakeRef(_gradient_png())}
    text = "before [[IMAGE:img1]] after"
    out, stats = await handlers._caption_images_into(text, images)
    assert out == text


async def test_caption_into_replaces_placeholder_with_caption(monkeypatch):
    vision = _FakeVision("A bar chart of quarterly sales.")
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    ref = _FakeRef(_gradient_png())
    images = {"img1": ref}
    out, stats = await handlers._caption_images_into("before [[IMAGE:img1]] after", images)
    assert out == "before [圖片描述：A bar chart of quarterly sales.] after"
    assert stats["image_count"] == 1
    assert stats["succeeded"] == 1
    assert stats["required"] == 1
    # The cleaned caption is stashed back on the ref for downstream persistence.
    assert ref.caption == "A bar chart of quarterly sales."
    assert len(vision.calls) == 1


async def test_caption_into_uniform_image_skipped_keeps_placeholder(monkeypatch):
    # Uniform-color image (PDF background fill) -> no VLM call, empty caption,
    # placeholder is preserved verbatim.
    vision = _FakeVision()
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    ref = _FakeRef(_solid_png((26, 54, 93)))
    images = {"u": ref}
    out, _stats = await handlers._caption_images_into("x [[IMAGE:u]] y", images)
    assert out == "x [[IMAGE:u]] y"
    assert vision.calls == []  # short-circuited before the VLM call
    assert ref.caption == ""


async def test_caption_into_oversized_image_skipped(monkeypatch):
    vision = _FakeVision()
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    # Force the size limit below our image size so the oversize branch fires.
    monkeypatch.setattr(settings, "vision_max_image_bytes", 1)
    ref = _FakeRef(_gradient_png())
    images = {"big": ref}
    out, _stats = await handlers._caption_images_into("a [[IMAGE:big]] b", images)
    assert out == "a [[IMAGE:big]] b"
    assert vision.calls == []


async def test_caption_into_vlm_failure_keeps_placeholder(monkeypatch):
    class _BoomVision:
        async def describe_image(self, image_bytes, mime="image/png"):
            raise RuntimeError("vlm down")

    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: _BoomVision())
    images = {"img1": _FakeRef(_gradient_png())}
    text = "p [[IMAGE:img1]] q"
    # A single failed caption must NOT raise — the placeholder survives.
    out, stats = await handlers._caption_images_into(text, images)
    assert out == text
    assert stats["image_count"] == 1
    assert stats["succeeded"] == 0
    assert stats["attempted"] == 1
    assert handlers.format_caption_progress(stats) == "1 張圖、0 張成功"
    assert handlers.format_caption_progress({"image_count": 0, "succeeded": 0}) == "0 張圖"
    assert handlers.format_caption_progress(
        {"image_count": 3, "succeeded": 0, "required": 0}
    ) == "3 張圖（未做圖說）"


async def test_repetitive_caption_is_not_success_and_stays_out_of_text(monkeypatch):
    """A non-empty loop must not increment succeeded or enter chunk text.

    Unfixed: succeeded = bool(cap) counted 601-char $\\text{}$ as success.
    """
    loop = "文字內容 (OCR)：** " + "$\\text{}" * 80
    vision = _FakeVision(loop)
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    ref = _FakeRef(_gradient_png())
    images = {"junk": ref}
    out, stats = await handlers._caption_images_into("see [[IMAGE:junk]] here", images)
    assert "[[IMAGE:junk]]" in out
    assert "圖片描述" not in out
    assert "$\\text{}" not in out
    assert stats["attempted"] == 1
    assert stats["succeeded"] == 0
    assert ref.caption == ""


async def test_coherent_truncated_caption_counts_and_is_marked(monkeypatch):
    body = (
        "圖片內容如下：左上角有一個圓形圖示，內有文字 4/26 Sun。"
        "中間是賽道示意圖，右側有海拔曲線。"
    ) * 6
    vision = _FakeVision(body)
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    ref = _FakeRef(_gradient_png())
    images = {"ok": ref}
    out, stats = await handlers._caption_images_into("see [[IMAGE:ok]] here", images)
    assert "圖片描述" in out
    assert stats["succeeded"] == 1
    assert "4/26" in (ref.caption or "")


async def test_caption_into_malformed_placeholder_left_as_is(monkeypatch):
    # Unterminated "[[IMAGE:" (no closing "]]") -> scanning stops, tail kept,
    # no infinite loop.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: _FakeVision())
    images = {"img1": _FakeRef(_gradient_png())}
    text = "head [[IMAGE:img1 tail-with-no-close"
    out, stats = await handlers._caption_images_into(text, images)
    assert out == text


async def test_caption_into_unknown_id_keeps_placeholder(monkeypatch):
    # Placeholder id not present in the images dict -> captions.get returns "",
    # so the original token shape is preserved.
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: _FakeVision())
    images = {"known": _FakeRef(_gradient_png())}
    text = "a [[IMAGE:unknown]] b"
    out, _stats = await handlers._caption_images_into(text, images)
    assert out == "a [[IMAGE:unknown]] b"


# ── _persist_images (infra-free early returns only) ───────────────────────────


async def test_persist_images_no_images_returns_empty_map():
    # Empty dict short-circuits before any pool / fs access.
    assert await handlers._persist_images(None, 1, 1, {}, None, None) == {}


async def test_persist_images_mkdir_failure_returns_empty_map(monkeypatch):
    # If the images dir can't be created, persistence bails with {} and never
    # touches the (None) pool — exercises the OSError guard.
    def _boom(*_a, **_k):
        raise OSError("read-only fs")

    monkeypatch.setattr(handlers.os, "makedirs", _boom)
    images = {"img1": _FakeRef(_gradient_png())}
    assert await handlers._persist_images(None, 1, 42, images, None, None) == {}


class _RecordingConn:
    """Records SQL. Does not execute it — engine behaviour is the PG test."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args):
        self.statements.append(sql)

    async def fetchval(self, sql: str, *args):
        self.statements.append(sql)
        return 77


class _RecordingPool:
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _conflict_assignment(sql: str, column: str) -> str:
    body = re.search(r"DO UPDATE\s+SET\s+(.*?)\s+RETURNING", sql, re.S | re.I)
    assert body, f"no DO UPDATE SET in:\n{sql}"
    text = re.sub(r"--[^\n]*", " ", body.group(1))
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    for part in parts:
        left, sep, right = part.partition("=")
        if sep and left.strip() == column:
            return " ".join(right.split())
    raise AssertionError(f"{column} missing from DO UPDATE SET:\n{sql}")


# EXCLUDED first: a NULL incoming value keeps the row; a real incoming
# value replaces it. The swapped form keeps the row forever.
_COALESCE_EXCLUDED_FIRST = re.compile(
    r"^COALESCE\(\s*EXCLUDED\.(?P<col>\w+)\s*,\s*ingestion_images\.(?P=col)\s*\)$"
)


def _assert_excluded_first_coalesce(rhs: str, column: str) -> None:
    assert _COALESCE_EXCLUDED_FIRST.match(rhs), (
        f"{column} must be COALESCE(EXCLUDED.{column}, ingestion_images.{column}); "
        f"got {rhs!r} — swapped args freeze the first vector forever"
    )


def test_coalesce_order_guard_rejects_swapped_and_bare_excluded():
    """The order regex is this file's only no-DB detector for the freeze mutant."""
    ok = "COALESCE(EXCLUDED.embedding, ingestion_images.embedding)"
    _assert_excluded_first_coalesce(ok, "embedding")
    with pytest.raises(AssertionError, match="swapped"):
        _assert_excluded_first_coalesce(
            "COALESCE(ingestion_images.embedding, EXCLUDED.embedding)",
            "embedding",
        )
    with pytest.raises(AssertionError, match="swapped"):
        _assert_excluded_first_coalesce("EXCLUDED.embedding", "embedding")


class _DeadEmbedder:
    """Embedder object is present; the HTTP call is not.

    Matches the production ``except Exception`` path in ``_persist_images``
    (embedder unavailable / batch failed) — ``embeddings`` becomes None and
    the upsert still runs. Deliberately has no ``model_name`` / ``native_dim``
    so EXCLUDED provenance is also NULL: the same shape as the vector.
    """

    async def embed(self, texts, *, user_id=None):
        raise RuntimeError("embedder unavailable")


class _LiveEmbedder:
    """Returns a canned vector and advertises provenance — the replace half."""

    def __init__(
        self,
        vec: list[float],
        *,
        model_name: str = "replacement/embedder",
        native_dim: int = 2048,
    ) -> None:
        self._vec = vec
        self.model_name = model_name
        self.native_dim = native_dim

    async def embed(self, texts, *, user_id=None):
        return [list(self._vec) for _ in texts]


def _halfvec_floats(value) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "to_list"):
        return [float(x) for x in value.to_list()]
    raise TypeError(f"expected HalfVector with to_list(), got {type(value)!r}")


def _assert_halfvec_close(got, expected, *, atol: float = 2e-3) -> None:
    got_f = _halfvec_floats(got)
    assert len(got_f) == len(expected)
    worst = max(abs(a - b) for a, b in zip(got_f, expected))
    assert worst <= atol, f"halfvec drifted {worst} (atol={atol})"


async def test_reimport_sql_keeps_existing_embedding_when_excluded_is_null(
    tmp_path, monkeypatch,
):
    """Dead embedder still upserts — but the SET must not clobber a live vector.

    This is the no-DB half of F-1. It fails on the unfixed assignment
    ``embedding = EXCLUDED.embedding`` (and the two provenance twins)
    **and** on swapped COALESCE args (``COALESCE(existing, EXCLUDED)``),
    which would freeze the first vector forever. It does **not** prove
    the engine keeps or replaces the bytes; those are the two PG tests.

    Mutant: drop COALESCE, or swap its two arguments → red.
    """
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    pool = _RecordingPool()
    ref = _FakeRef(_gradient_png())
    ref.caption = "reimport caption, embedder down"
    pks = await handlers._persist_images(
        pool, 7, 42, {"fig-keep": ref}, _DeadEmbedder(), None,
    )
    assert pks == {"fig-keep": 77}

    upserts = [s for s in pool.conn.statements if "ON CONFLICT" in s]
    assert len(upserts) == 1, pool.conn.statements
    sql = upserts[0]
    for column in (
        "embedding", "embedding_source_model", "embedding_native_dim",
        "caption_source_model",
    ):
        _assert_excluded_first_coalesce(
            _conflict_assignment(sql, column), column,
        )


_PG_DSN = os.environ.get("ANILA_TEST_PG_DSN")
# Without this DSN the two live tests skip. Directions ② (drop COALESCE)
# and ③ (swap COALESCE args) then go red only via the *text* guard
# (``test_reimport_sql_…`` / ``_assert_excluded_first_coalesce``) — the
# behavioural pair is gated. "It went red once with a DSN" is not the
# default path. Run this file with ANILA_TEST_PG_DSN set.
_SEED_VEC = [0.11, 0.22, 0.33, 0.44] + [0.01] * 3996
_NEW_VEC = [0.91, 0.82, 0.73, 0.64] + [0.02] * 3996


class _CspAppPool:
    """Same physical DSN, production role.

    The documented live-PG DSN is often the bypassrls ``csp`` user. Under
    that role, dropping ``SET LOCAL anila.collection_id`` is invisible —
    FORCE RLS never applies. Production uses ``csp_app`` (NOBYPASSRLS).
    Seed / cleanup stay on the login role so CASCADE and leftover counts
    are not tautological.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def acquire(self):
        return _CspAppAcquire(self._inner.acquire())


class _CspAppAcquire:
    def __init__(self, inner_cm) -> None:
        self._inner_cm = inner_cm
        self._conn = None

    async def __aenter__(self):
        self._conn = await self._inner_cm.__aenter__()
        try:
            await self._conn.execute("SET ROLE csp_app")
            role = await self._conn.fetchval("SELECT current_user")
            bypass = await self._conn.fetchval(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            assert role == "csp_app" and bypass is False, (
                f"persist must run as NOBYPASSRLS csp_app; got {role!r} "
                f"bypassrls={bypass!r}"
            )
            return self._conn
        except BaseException:
            await self._inner_cm.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, *exc):
        try:
            await self._conn.execute("RESET ROLE")
        finally:
            return await self._inner_cm.__aexit__(*exc)


async def _seed_image_row(pool, marker: str, image_id: str):
    from pgvector import HalfVector

    async with pool.acquire() as conn:
        collection_id = await conn.fetchval(
            "INSERT INTO ingestion_collections "
            "(name, embedding_model, created_by) "
            "VALUES ($1, $2, $3) RETURNING id",
            marker, "nvidia/nv-embed-v2", 1,
        )
        document_id = await conn.fetchval(
            "INSERT INTO ingestion_documents "
            "(collection_id, filename, sha256) "
            "VALUES ($1, $2, $3) RETURNING id",
            collection_id, f"{marker}.pdf", "ab" * 32,
        )
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            seeded_pk = await conn.fetchval(
                """
                INSERT INTO ingestion_images
                    (collection_id, document_id, image_id, storage_path,
                     mime, caption, bytes_size, embedding,
                     embedding_source_model, embedding_native_dim)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                collection_id, document_id, image_id,
                f"anila-images/{document_id}/{image_id}.png",
                "image/png", "original caption", 99,
                HalfVector(_SEED_VEC), "nvidia/nv-embed-v2", 4096,
            )
    return collection_id, document_id, seeded_pk


async def _fetch_embedding_row(pool, collection_id: int, pk: int):
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"SET LOCAL anila.collection_id = {int(collection_id)}"
        )
        return await conn.fetchrow(
            """
            SELECT embedding, embedding_source_model, embedding_native_dim
              FROM ingestion_images WHERE id = $1
            """,
            pk,
        )


async def _assert_no_f1_residue(pool, marker: str) -> None:
    """Leftover of this run must be 0; the same query must also see live data.

    ``ingestion_images`` is FORCE RLS. A count of 0 with no GUC, on a
    NOBYPASSRLS role, is tautological. The positive anchor is therefore
    ``count(*) FROM ingestion_images`` — not ``users`` (that table has
    no RLS, so it would bless a 0 that the images table itself hid).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM ingestion_collections
                WHERE name = $1) AS leftover_collections,
              (SELECT count(*) FROM ingestion_documents
                WHERE filename = $2) AS leftover_documents,
              (SELECT count(*) FROM ingestion_images i
                 JOIN ingestion_documents d ON d.id = i.document_id
                WHERE d.filename = $2) AS leftover_images,
              (SELECT count(*) FROM ingestion_images) AS visible_images
            """,
            marker, f"{marker}.pdf",
        )
    assert row["visible_images"] > 0, (
        "leftover-0 is untrusted: this connection cannot see "
        "ingestion_images rows (FORCE RLS / wrong role?)"
    )
    assert row["leftover_collections"] == 0, row
    assert row["leftover_documents"] == 0, row
    assert row["leftover_images"] == 0, row


@pytest.mark.skipif(
    not _PG_DSN,
    reason="ANILA_TEST_PG_DSN not set — ON CONFLICT / halfvec is Postgres-only",
)
async def test_reimport_without_embedder_keeps_existing_embedding(tmp_path, monkeypatch):
    """embedder 不可用時重匯入一次，既有 embedding 必須還在.

    Not "the upsert runs". HalfVector is not iterable and halfvec is
    float16 — compare via ``to_list()`` + tolerance, or this stays red
    even when the fix is correct.

    Mutant: bare ``embedding = EXCLUDED.embedding`` → NULL → red at
    ``embedding is not None``.
    """
    from anila_core.storage.adapters.pg_pool import PgPool

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_id = "fig-keep"
    marker = f"_f1_reimport_{uuid.uuid4().hex[:12]}"
    pool = PgPool(_PG_DSN, min_size=1, max_size=1)
    await pool.open()
    collection_id = None
    try:
        collection_id, document_id, seeded_pk = await _seed_image_row(
            pool, marker, image_id,
        )
        ref = _FakeRef(_gradient_png())
        ref.caption = "reimport caption, embedder down"
        pks = await handlers._persist_images(
            _CspAppPool(pool), collection_id, document_id,
            {image_id: ref}, _DeadEmbedder(), None,
        )
        assert pks.get(image_id) == seeded_pk
        kept = await _fetch_embedding_row(pool, collection_id, seeded_pk)
        assert kept is not None
        assert kept["embedding"] is not None, (
            "re-import with a dead embedder wiped the existing embedding"
        )
        _assert_halfvec_close(kept["embedding"], _SEED_VEC)
        assert kept["embedding_source_model"] == "nvidia/nv-embed-v2"
        assert kept["embedding_native_dim"] == 4096
    finally:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM ingestion_collections WHERE name = $1",
                    marker,
                )
            await _assert_no_f1_residue(pool, marker)
        finally:
            await pool.close()


@pytest.mark.skipif(
    not _PG_DSN,
    reason="ANILA_TEST_PG_DSN not set — ON CONFLICT / halfvec is Postgres-only",
)
async def test_reimport_with_embedder_replaces_existing_embedding(tmp_path, monkeypatch):
    """embedder 正常時重匯入一次，向量必須真的被換掉.

    Swapping COALESCE args (``COALESCE(existing, EXCLUDED)``) freezes the
    first vector forever. Caption / path / size still update, no log, no
    red — unless this direction exists.

    Mutant: swapped COALESCE → this stays on ``_SEED_VEC`` → red.
    Dropped COALESCE + dead embedder is the other test; this one is the
    replace half.
    """
    from anila_core.storage.adapters.pg_pool import PgPool

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_id = "fig-replace"
    marker = f"_f1_reimport_{uuid.uuid4().hex[:12]}"
    pool = PgPool(_PG_DSN, min_size=1, max_size=1)
    await pool.open()
    try:
        collection_id, document_id, seeded_pk = await _seed_image_row(
            pool, marker, image_id,
        )
        ref = _FakeRef(_gradient_png())
        ref.caption = "reimport caption, embedder live"
        pks = await handlers._persist_images(
            _CspAppPool(pool), collection_id, document_id,
            {image_id: ref}, _LiveEmbedder(_NEW_VEC), None,
        )
        assert pks.get(image_id) == seeded_pk
        got = await _fetch_embedding_row(pool, collection_id, seeded_pk)
        assert got is not None
        assert got["embedding"] is not None
        _assert_halfvec_close(got["embedding"], _NEW_VEC)
        seed_f = _halfvec_floats(got["embedding"])
        assert max(abs(a - b) for a, b in zip(seed_f, _SEED_VEC)) > 0.5, (
            "vector still matches the first insert — COALESCE args swapped?"
        )
        assert got["embedding_source_model"] == "replacement/embedder"
        assert got["embedding_native_dim"] == 2048
    finally:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM ingestion_collections WHERE name = $1",
                    marker,
                )
            await _assert_no_f1_residue(pool, marker)
        finally:
            await pool.close()


async def test_caption_into_embed_text_has_no_url_or_image_markup(monkeypatch):
    """Embed text keeps the caption and must not grow a URL or markdown image."""
    vision = _FakeVision("轉換區示意圖，左進右出。")
    monkeypatch.setattr(handlers, "_get_vision_provider", lambda model=None: vision)
    images = {"fig1": _FakeRef(_gradient_png())}
    out, _stats = await handlers._caption_images_into(
        "見 [[IMAGE:fig1]] 如圖。", images,
    )
    assert "[圖片描述：轉換區示意圖，左進右出。]" in out
    assert "[[IMAGE:" not in out
    assert "http://" not in out
    assert "https://" not in out
    assert "/api/ingestion" not in out
    assert "![" not in out
    assert "](" not in out


def test_attach_image_pks_puts_pk_on_metadata_not_in_content():
    from anila_core.ingestion.chunking_plugins.base import ChunkResult

    content = "見 [圖片描述：轉換區示意圖，左進右出。] 如圖。"
    chunk = ChunkResult(
        content=content,
        chunk_key="doc/leaf-1",
        token_count=12,
        metadata={"strategy": "hierarchical", "chunk_type": "leaf"},
    )
    ref = _FakeRef(_gradient_png())
    ref.caption = "轉換區示意圖，左進右出。"
    out = handlers._attach_image_pks_to_chunks(
        [chunk], {"fig1": ref}, {"fig1": 42},
    )
    assert len(out) == 1
    assert out[0].content == content
    assert "http" not in out[0].content
    assert "/api/" not in out[0].content
    assert "![" not in out[0].content
    assert out[0].metadata["image_pks"] == [42]
    assert out[0].metadata["strategy"] == "hierarchical"


def test_attach_image_pks_skips_chunks_without_figures():
    from anila_core.ingestion.chunking_plugins.base import ChunkResult

    chunk = ChunkResult(
        content="這段只有文字，沒有圖。",
        chunk_key="doc/leaf-2",
        token_count=8,
        metadata={"chunk_type": "leaf"},
    )
    ref = _FakeRef(_gradient_png())
    ref.caption = "轉換區示意圖"
    out = handlers._attach_image_pks_to_chunks(
        [chunk], {"fig1": ref}, {"fig1": 7},
    )
    assert out[0].content == chunk.content
    assert "image_pks" not in out[0].metadata
