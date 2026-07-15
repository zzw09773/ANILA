"""Unit tests for pure / easily-mockable helpers in ``ingestion_worker.handlers``.

Scope (deliberately narrow — no DB/redis/arq infra stood up):
  * ``_clean_caption``        — pure string transform (reasoning-preamble strip,
                                 paragraph selection, whitespace collapse, truncation).
  * ``_get_vision_provider``  — config-gated lazy factory (the two "return None"
                                 fast paths; we never construct a real VisionProvider).
  * ``_caption_images_into``  — async placeholder rewrite, exercised with the vision
                                 provider monkeypatched to a fake (no network).
  * ``_persist_images``       — filesystem and DB failure paths plus the
                                 explicit embedding-degradation and legal skip
                                 paths, all with fakes.

``_is_uniform_color`` is intentionally NOT tested here — tests/test_uniform_color.py
already owns it.

The narrow terminal-state branches of ``ingest_document`` are covered with
mocked DB helpers; no live PgPool / asyncpg connection or Embedder endpoint is
needed here.
"""
from __future__ import annotations

import asyncio
import errno
import io
import stat
from types import SimpleNamespace

import asyncpg
import pytest
from PIL import Image

from anila_core.contracts import Classification
from anila_core.ingestion.errors import EmbedError, StoreError
from ingestion_worker import evaluator, handlers
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


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeConnection:
    def __init__(
        self,
        *,
        fail_after: int | None = None,
        failure: Exception | None = None,
    ):
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_after = fail_after
        self.failure = failure or RuntimeError("database unavailable")

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql: str, *args: object):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise self.failure
        self.calls.append((sql, args))


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    def acquire(self):
        return _FakeAcquire(self.connection)


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


# ── _persist_images fail-closed persistence contract ─────────────────────────


async def test_persist_images_no_images_returns_zero():
    # Empty dict short-circuits before any pool / fs access.
    assert await handlers._persist_images(None, 1, 1, {}, None, None) == 0


async def test_persist_images_mkdir_enospc_is_retryable_storage_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space at /sensitive/upload/path")

    monkeypatch.setattr(handlers.os, "makedirs", _boom)
    images = {"img1": _FakeRef(_gradient_png())}
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(None, 1, 42, images, None, None)

    error = exc_info.value
    assert error.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert error.retryable is True
    assert error.details == {"operation": "mkdir", "errno": "ENOSPC"}
    assert "/sensitive" not in str(error)


async def test_persist_images_open_enospc_is_retryable_storage_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _open_boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space at /sensitive/upload/path")

    monkeypatch.setattr(handlers, "open", _open_boom, raising=False)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            None, 1, 42, {"img1": _FakeRef(_gradient_png())}, None, None
        )

    assert exc_info.value.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"operation": "write", "errno": "ENOSPC"}


async def test_persist_images_write_enospc_is_retryable_storage_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    class FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def write(self, _data):
            raise OSError(errno.ENOSPC, "write failed")

    monkeypatch.setattr(handlers, "open", lambda *_a, **_k: FailingFile(), raising=False)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            None, 1, 42, {"img1": _FakeRef(_gradient_png())}, None, None
        )

    assert exc_info.value.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"operation": "write", "errno": "ENOSPC"}


async def test_persist_images_chmod_failure_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _chmod_boom(*_a, **_k):
        raise OSError(errno.EROFS, "read-only filesystem")

    monkeypatch.setattr(handlers.os, "chmod", _chmod_boom)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            None, 1, 42, {"img1": _FakeRef(_gradient_png())}, None, None
        )

    assert exc_info.value.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"operation": "chmod", "errno": "EROFS"}
    assert list(tmp_path.rglob("*.png")) == []


async def test_persist_images_db_rls_failure_is_retryable_and_cleans_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    connection = _FakeConnection(
        fail_after=1,
        failure=RuntimeError("RLS denied /sensitive/database/details"),
    )

    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(connection),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_PERSISTENCE_FAILED"
    assert error.retryable is True
    assert error.details["operation"] == "db_insert"
    assert error.details["rows_attempted"] == 1
    assert error.details["rows_inserted"] == 0
    assert "/sensitive" not in str(error)
    assert list(tmp_path.rglob("*.png")) == []


async def test_persist_images_db_rls_violation_is_critical_and_cleans_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    connection = _FakeConnection(
        fail_after=1,
        failure=asyncpg.exceptions.InsufficientPrivilegeError(
            "RLS denied /sensitive/database/details"
        ),
    )

    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(connection),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_PG_RLS_VIOLATION"
    assert error.retryable is False
    assert error.severity == "critical"
    assert error.details == {
        "operation": "db_insert",
        "reason": "rls_violation",
        "rows_attempted": 1,
        "rows_inserted": 0,
    }
    assert "/sensitive" not in str(error)
    assert "RLS denied" not in str(error)
    assert list(tmp_path.rglob("*.tmp-*")) == []


async def test_persist_images_db_failure_preserves_preexisting_final_file(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    final_path = tmp_path / "anila-images" / "42" / "img1.png"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-good-image")
    connection = _FakeConnection(fail_after=1)

    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(connection),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    assert exc_info.value.code == "E_IMAGE_PERSISTENCE_FAILED"
    assert final_path.read_bytes() == b"previous-good-image"
    assert list(tmp_path.rglob("*.tmp-*")) == []


async def test_persist_images_publish_failure_restores_all_previous_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    final_one = image_root / "img1.png"
    final_two = image_root / "img2.png"
    final_one.write_bytes(b"previous-image-one")
    final_two.write_bytes(b"previous-image-two")
    connection = _FakeConnection()
    real_replace = handlers.os.replace
    temp_publishes = 0

    def _replace_with_second_publish_failure(source, destination):
        nonlocal temp_publishes
        if ".tmp-" in str(source):
            temp_publishes += 1
            if temp_publishes == 2:
                raise OSError(errno.EIO, "publish I/O failure")
        return real_replace(source, destination)

    monkeypatch.setattr(handlers.os, "replace", _replace_with_second_publish_failure)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(connection),
            7,
            42,
            {
                "img1": _FakeRef(_gradient_png()),
                "img2": _FakeRef(_gradient_png()),
            },
            None,
            None,
        )

    assert exc_info.value.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"operation": "publish", "errno": "EIO"}
    assert final_one.read_bytes() == b"previous-image-one"
    assert final_two.read_bytes() == b"previous-image-two"
    assert list(tmp_path.rglob("*.tmp-*")) == []
    assert list(tmp_path.rglob("*.bak-*")) == []


async def test_persist_images_publish_non_oserror_is_not_misclassified(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    final_path = tmp_path / "anila-images" / "42" / "img1.png"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-good-image")
    real_replace = handlers.os.replace

    def _replace_bug(source, destination):
        if ".tmp-" in str(source):
            raise RuntimeError("unexpected publish bug")
        return real_replace(source, destination)

    monkeypatch.setattr(handlers.os, "replace", _replace_bug)
    with pytest.raises(RuntimeError, match="unexpected publish bug"):
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    assert final_path.read_bytes() == b"previous-good-image"
    assert list(tmp_path.rglob("*.tmp-*")) == []
    assert list(tmp_path.rglob("*.bak-*")) == []


async def test_persist_images_backup_cleanup_failure_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    final_path = tmp_path / "anila-images" / "42" / "img1.png"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-good-image")
    new_bytes = _gradient_png()
    real_unlink = handlers.os.unlink

    def _unlink_backup_failure(path):
        if ".bak-" in str(path):
            raise OSError(errno.EIO, "backup cleanup I/O failure")
        return real_unlink(path)

    monkeypatch.setattr(handlers.os, "unlink", _unlink_backup_failure)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(new_bytes)},
            None,
            None,
        )

    assert exc_info.value.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"operation": "publish", "errno": "EIO"}
    assert final_path.read_bytes() == new_bytes
    backups = list(tmp_path.rglob("*.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous-good-image"


async def test_persist_images_reconciles_stale_backup_before_replacing_final(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    final_path = tmp_path / "anila-images" / "42" / "img1.png"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"current-image")
    (final_path.parent / "img1.png.bak-stale").write_bytes(b"stale-copy")
    new_bytes = _gradient_png()

    result = await handlers._persist_images(
        _FakePool(_FakeConnection()),
        7,
        42,
        {"img1": _FakeRef(new_bytes)},
        None,
        None,
    )

    assert result == 1
    assert final_path.read_bytes() == new_bytes
    assert list(tmp_path.rglob("*.bak-*")) == []


async def test_persist_images_restores_unique_backup_when_final_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    final_path = image_root / "img1.png"
    (image_root / "img1.png.bak-recoverable").write_bytes(b"recoverable-image")
    new_bytes = _gradient_png()

    result = await handlers._persist_images(
        _FakePool(_FakeConnection()),
        7,
        42,
        {"img1": _FakeRef(new_bytes)},
        None,
        None,
    )

    assert result == 1
    assert final_path.read_bytes() == new_bytes
    assert list(tmp_path.rglob("*.bak-*")) == []


async def test_persist_images_ambiguous_backups_fail_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    final_path = image_root / "img1.png"
    backup_one = image_root / "img1.png.bak-one"
    backup_two = image_root / "img1.png.bak-two"
    backup_one.write_bytes(b"candidate-one")
    backup_two.write_bytes(b"candidate-two")

    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    assert exc_info.value.code == "E_IMAGE_RECONCILIATION_FAILED"
    assert exc_info.value.retryable is False
    assert exc_info.value.severity == "critical"
    assert exc_info.value.details == {
        "operation": "reconcile",
        "reason": "ambiguous_backups",
    }
    assert not final_path.exists()
    assert backup_one.read_bytes() == b"candidate-one"
    assert backup_two.read_bytes() == b"candidate-two"
    assert list(tmp_path.rglob("*.tmp-*")) == []


@pytest.mark.parametrize(
    "unsafe_mode",
    [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO],
)
async def test_persist_images_unsafe_backup_fails_closed(
    monkeypatch, tmp_path, unsafe_mode
):
    """Backup residue must never be followed, recursed into, or restored."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    backup_path = image_root / "img1.png.bak-unsafe"
    backup_path.write_bytes(b"candidate")
    real_lstat = handlers.os.lstat

    def _lstat_with_unsafe_backup(path):
        if str(path) == str(backup_path):
            return SimpleNamespace(st_mode=unsafe_mode)
        return real_lstat(path)

    monkeypatch.setattr(handlers.os, "lstat", _lstat_with_unsafe_backup)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_RECONCILIATION_FAILED"
    assert error.retryable is False
    assert error.severity == "critical"
    assert error.details == {
        "operation": "reconcile",
        "reason": "unsafe_backup",
    }
    assert backup_path.exists()
    assert list(tmp_path.rglob("*.tmp-*")) == []


@pytest.mark.parametrize(
    "unsafe_mode",
    [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO],
)
async def test_persist_images_unsafe_final_fails_closed(
    monkeypatch, tmp_path, unsafe_mode
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    final_path = image_root / "img1.png"
    final_path.write_bytes(b"current")
    backup_path = image_root / "img1.png.bak-unsafe-final"
    backup_path.write_bytes(b"previous")
    real_lstat = handlers.os.lstat

    def _lstat_with_unsafe_final(path):
        if str(path) == str(final_path):
            return SimpleNamespace(st_mode=unsafe_mode)
        return real_lstat(path)

    monkeypatch.setattr(handlers.os, "lstat", _lstat_with_unsafe_final)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_RECONCILIATION_FAILED"
    assert error.retryable is False
    assert error.severity == "critical"
    assert error.details == {
        "operation": "reconcile",
        "reason": "unsafe_final",
    }
    assert final_path.read_bytes() == b"current"
    assert backup_path.read_bytes() == b"previous"
    assert list(tmp_path.rglob("*.tmp-*")) == []


async def test_persist_images_reconciles_crash_temp_before_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    final_path = image_root / "img1.png"
    stale_temp = image_root / "img1.png.tmp-crashed"
    stale_temp.write_bytes(b"partial-crash-residue")
    new_bytes = _gradient_png()

    result = await handlers._persist_images(
        _FakePool(_FakeConnection()),
        7,
        42,
        {"img1": _FakeRef(new_bytes)},
        None,
        None,
    )

    assert result == 1
    assert final_path.read_bytes() == new_bytes
    assert not stale_temp.exists()
    assert list(tmp_path.rglob("*.tmp-*")) == []


async def test_persist_images_unsafe_temp_residue_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    stale_temp = image_root / "img1.png.tmp-unsafe"
    stale_temp.write_bytes(b"partial-crash-residue")
    real_lstat = handlers.os.lstat

    def _lstat_with_unsafe_temp(path):
        if str(path) == str(stale_temp):
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real_lstat(path)

    monkeypatch.setattr(handlers.os, "lstat", _lstat_with_unsafe_temp)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_RECONCILIATION_FAILED"
    assert error.retryable is False
    assert error.severity == "critical"
    assert error.details == {
        "operation": "reconcile",
        "reason": "unsafe_temp_residue",
    }
    assert stale_temp.exists()
    assert list(tmp_path.rglob("*.tmp-*")) == [stale_temp]


async def test_persist_images_unsafe_image_directory_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    real_lstat = handlers.os.lstat

    def _lstat_with_unsafe_root(path):
        if str(path) == str(image_root):
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real_lstat(path)

    monkeypatch.setattr(handlers.os, "lstat", _lstat_with_unsafe_root)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_RECONCILIATION_FAILED"
    assert error.retryable is False
    assert error.severity == "critical"
    assert error.details == {
        "operation": "reconcile",
        "reason": "unsafe_image_directory",
    }
    assert list(tmp_path.rglob("*.tmp-*")) == []


async def test_persist_images_temp_reconcile_failure_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    image_root = tmp_path / "anila-images" / "42"
    image_root.mkdir(parents=True)
    stale_temp = image_root / "img1.png.tmp-crashed"
    stale_temp.write_bytes(b"partial-crash-residue")
    real_unlink = handlers.os.unlink

    def _unlink_failure(path):
        if ".tmp-" in str(path):
            raise OSError(errno.EIO, "temp cleanup failed at /sensitive/path")
        return real_unlink(path)

    monkeypatch.setattr(handlers.os, "unlink", _unlink_failure)
    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(_FakeConnection()),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    error = exc_info.value
    assert error.code == "E_IMAGE_STORAGE_UNAVAILABLE"
    assert error.retryable is True
    assert error.details == {"operation": "reconcile", "errno": "EIO"}
    assert stale_temp.exists()


async def test_persist_images_cleanup_failure_does_not_mask_db_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    connection = _FakeConnection(fail_after=1)
    monkeypatch.setattr(
        handlers.os,
        "unlink",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EIO, "cleanup")),
    )

    with pytest.raises(StoreError) as exc_info:
        await handlers._persist_images(
            _FakePool(connection),
            7,
            42,
            {"img1": _FakeRef(_gradient_png())},
            None,
            None,
        )

    assert exc_info.value.code == "E_IMAGE_PERSISTENCE_FAILED"


async def test_persist_images_unexpected_filesystem_exception_is_not_misclassified(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _open_bug(*_a, **_k):
        raise RuntimeError("unexpected writer bug")

    monkeypatch.setattr(handlers, "open", _open_bug, raising=False)
    with pytest.raises(RuntimeError, match="unexpected writer bug"):
        await handlers._persist_images(
            None, 1, 42, {"img1": _FakeRef(_gradient_png())}, None, None
        )


async def test_persist_images_embedding_failure_keeps_row_with_null_embedding(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    connection = _FakeConnection()
    ref = _FakeRef(_gradient_png())
    ref.caption = "chart caption"

    class FailingEmbedder:
        async def embed(self, *_args, **_kwargs):
            raise RuntimeError("embedding service unavailable")

    result = await handlers._persist_images(
        _FakePool(connection),
        7,
        42,
        {"img1": ref},
        FailingEmbedder(),
        None,
    )

    assert result == 1
    assert len(connection.calls) == 2
    insert_args = connection.calls[1][1]
    assert insert_args[7] == "chart caption"
    assert insert_args[-1] is None
    assert list(tmp_path.rglob("*.png"))
    assert list(tmp_path.rglob("*.tmp-*")) == []
    assert list(tmp_path.rglob("*.bak-*")) == []


async def test_persist_images_empty_and_uniform_images_are_legal_skips(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _mkdir_boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(handlers.os, "makedirs", _mkdir_boom)
    images = {
        "empty": _FakeRef(b""),
        "uniform": _FakeRef(_solid_png((26, 54, 93))),
    }
    assert await handlers._persist_images(None, 1, 42, images, None, None) == 0


async def test_ingest_missing_blob_error_does_not_expose_storage_path(
    monkeypatch, tmp_path
):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )
    monkeypatch.setattr(handlers.os.path, "exists", lambda _path: False)

    with pytest.raises(StoreError) as exc_info:
        await handlers.ingest_document(state["ctx"], document_id=41)

    error = exc_info.value
    assert error.user_message == "上傳檔案目前無法使用。"
    assert error.details == {"reason": "missing_blob", "has_path": True}
    assert str(tmp_path) not in error.user_message
    assert str(tmp_path) not in str(error.details)



@pytest.mark.parametrize(
    ("document_level", "collection_level"),
    [(document, collection) for document in Classification for collection in Classification],
)
def test_effective_chunk_classification_uses_canonical_max_truth_table(
    document_level: Classification,
    collection_level: Classification,
) -> None:
    effective = handlers._effective_chunk_classification(
        {
            "document_classification_level": document_level.to_storage(),
            "collection_classification_level": collection_level.to_storage(),
        }
    )

    assert effective is Classification.max_of([document_level, collection_level])


@pytest.mark.parametrize("bad_value", [None, "UNKNOWN", 3, ""])
@pytest.mark.parametrize(
    "field",
    ["document_classification_level", "collection_classification_level"],
)
def test_effective_chunk_classification_rejects_missing_or_invalid_storage(
    field: str,
    bad_value: object,
) -> None:
    meta: dict[str, object] = {
        "document_classification_level": Classification.UNCLASSIFIED.to_storage(),
        "collection_classification_level": Classification.UNCLASSIFIED.to_storage(),
    }
    meta[field] = bad_value

    with pytest.raises(StoreError) as exc_info:
        handlers._effective_chunk_classification(meta)

    assert exc_info.value.code == "E_CLASSIFICATION_INVALID"
    assert exc_info.value.retryable is False
    assert exc_info.value.severity == "critical"


async def test_load_document_meta_selects_document_and_collection_classification() -> None:
    class Connection:
        sql = ""

        async def fetchrow(self, sql, document_id):
            self.sql = sql
            assert document_id == 17
            return {
                "document_id": 17,
                "document_classification_level": "機密",
                "collection_classification_level": "極機密",
            }

    class Acquire:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback) -> bool:
            return False

    class Pool:
        def __init__(self, connection) -> None:
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    connection = Connection()
    meta = await handlers._load_document_meta(Pool(connection), 17)  # type: ignore[arg-type]

    assert "d.classification_level AS document_classification_level" in connection.sql
    assert "c.classification_level AS collection_classification_level" in connection.sql
    assert meta["document_classification_level"] == "機密"
    assert meta["collection_classification_level"] == "極機密"


# ── ingest_document terminal convergence ─────────────────────────────────────


def _install_ingest_fakes(
    monkeypatch,
    tmp_path,
    *,
    chunks,
    embedder,
    document_classification: str = "無機密",
    collection_classification: str = "無機密",
):
    fingerprint = "sha256:" + ("1" * 64)
    monkeypatch.setattr(
        handlers.settings, "embedding_model_fingerprint", fingerprint
    )
    blob_path = tmp_path / "document.txt"
    blob_path.write_text("content", encoding="utf-8")
    document_updates: list[tuple[str, dict]] = []
    job_updates: list[dict] = []
    failures = []
    replacements: list[dict] = []

    async def load_meta(_pool, _document_id):
        return {
            "collection_id": 7,
            "storage_path": str(blob_path),
            "uploaded_by": 11,
            "owner_user_id": 12,
            "filename": "document.txt",
            "mime_type": "text/plain",
            "document_classification_level": document_classification,
            "collection_classification_level": collection_classification,
            "chunking_config": {"strategy": "test", "params": {}},
            "embedding_model": handlers.settings.embedding_model,
            "embedding_fingerprint": fingerprint,
            "embedding_dim": handlers.settings.embedding_dim,
        }

    async def update_document(_pool, _document_id, status, **kwargs):
        document_updates.append((status, kwargs))

    async def update_job(_pool, _job_id, **kwargs):
        job_updates.append(kwargs)

    async def record_failure(_pool, _job_id, error):
        failures.append(error)

    async def reconcile_counters(_pool, _collection_id):
        return None

    async def load_authority_user(
        _pool, *, ingestion_job_id, fallback_user_id
    ):
        del ingestion_job_id, fallback_user_id
        return 11

    async def require_eval_data_clearance(
        _pool, *, user_id, collection_id, document_ids
    ):
        del user_id, collection_id, document_ids
        return None

    class RecordingStore:
        def __init__(self, _pool, *, collection_id: int) -> None:
            assert collection_id == 7

        async def replace_document_chunks(self, **kwargs):
            replacements.append(kwargs)
            return len(kwargs["parent_chunks"]) + len(kwargs["leaf_chunks"])

        async def stage_and_activate_generation(self, **kwargs):
            replacements.append(kwargs)
            return len(kwargs["parent_chunks"]) + len(kwargs["leaf_chunks"])

    chunker = SimpleNamespace(requires_embedder=False, chunk=lambda *_args: chunks)
    monkeypatch.setattr(handlers, "_load_document_meta", load_meta)
    monkeypatch.setattr(handlers, "_update_document_status", update_document)
    monkeypatch.setattr(handlers, "_update_job", update_job)
    monkeypatch.setattr(handlers, "_record_job_failure", record_failure)
    monkeypatch.setattr(
        handlers, "_reconcile_collection_counters", reconcile_counters
    )
    monkeypatch.setattr(
        handlers, "_load_ingestion_authority_user", load_authority_user
    )
    monkeypatch.setattr(
        evaluator, "_require_eval_data_clearance", require_eval_data_clearance
    )
    monkeypatch.setattr(
        handlers, "CollectionScopedPgVectorStore", RecordingStore
    )
    async def read_and_extract(*_args, **_kwargs):
        return "parsed content", {}, {}

    monkeypatch.setattr(handlers, "read_and_extract", read_and_extract)
    monkeypatch.setattr(handlers, "get_chunker", lambda _strategy: chunker)
    return {
        "ctx": {"pool": object(), "embedder": embedder, "job_id": "job-1"},
        "document_updates": document_updates,
        "job_updates": job_updates,
        "failures": failures,
        "replacements": replacements,
    }


async def test_ingest_rejects_invalid_queue_proof_before_pool_access(monkeypatch):
    monkeypatch.setattr(handlers.settings, "ingestion_queue_hmac_key", "k" * 32)

    with pytest.raises(PermissionError, match="integrity proof"):
        await handlers.ingest_document(
            {"pool": object(), "embedder": object()},
            document_id=41,
            ingestion_job_id=9,
            queue_proof="invalid",
        )


async def test_ingest_clearance_denial_precedes_raw_document_read(
    monkeypatch, tmp_path
):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )
    raw_read = False

    async def deny_clearance(*_args, **_kwargs):
        raise PermissionError("live clearance denied")

    async def forbidden_raw_read(*_args, **_kwargs):
        nonlocal raw_read
        raw_read = True
        raise AssertionError("raw document was read before authorization")

    monkeypatch.setattr(evaluator, "_require_eval_data_clearance", deny_clearance)
    monkeypatch.setattr(handlers, "read_and_extract", forbidden_raw_read)

    with pytest.raises(PermissionError, match="live clearance denied"):
        await handlers.ingest_document(state["ctx"], document_id=41)

    assert raw_read is False


async def test_ingest_propagates_image_persistence_store_error(
    monkeypatch, tmp_path
):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )
    image_ref = _FakeRef(_gradient_png())

    async def read_with_image(*_args, **_kwargs):
        return "parsed [[IMAGE:img1]]", {}, {"img1": image_ref}

    async def fail_persist(*_args, **_kwargs):
        raise StoreError(
            code="E_IMAGE_PERSISTENCE_FAILED",
            retryable=True,
            severity="error",
            user_message="圖片資料寫入失敗，系統將自動重試。",
        )

    monkeypatch.setattr(handlers, "read_and_extract", read_with_image)
    monkeypatch.setattr(handlers, "_persist_images", fail_persist)

    with pytest.raises(StoreError) as exc_info:
        await handlers.ingest_document(state["ctx"], document_id=41)

    assert exc_info.value.code == "E_IMAGE_PERSISTENCE_FAILED"
    assert state["failures"][0].code == "E_IMAGE_PERSISTENCE_FAILED"
    assert state["document_updates"][-1] == (
        "failed",
        {"error_message": "圖片資料寫入失敗，系統將自動重試。"},
    )
    assert state["replacements"] == []


async def test_reresolve_rejects_invalid_queue_proof_before_pool_access(
    monkeypatch,
):
    monkeypatch.setattr(handlers.settings, "ingestion_queue_hmac_key", "k" * 32)

    with pytest.raises(PermissionError, match="integrity proof"):
        await handlers.reresolve_collection_relations(
            {"pool": object()},
            collection_id=7,
            actor_user_id=11,
            queue_proof="invalid",
        )


async def test_reresolve_clearance_denial_precedes_raw_document_read(
    monkeypatch, tmp_path
):
    blob_path = tmp_path / "classified.txt"
    blob_path.write_text("classified", encoding="utf-8")
    raw_read = False

    class Connection:
        async def fetch(self, *_args, **_kwargs):
            return [
                {
                    "id": 41,
                    "filename": "classified.txt",
                    "mime_type": "text/plain",
                    "storage_path": str(blob_path),
                }
            ]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def deny_clearance(*_args, **_kwargs):
        raise PermissionError("live clearance denied")

    async def forbidden_raw_read(*_args, **_kwargs):
        nonlocal raw_read
        raw_read = True
        raise AssertionError("raw document was read before authorization")

    monkeypatch.setattr(handlers.settings, "ingestion_queue_hmac_key", "")
    monkeypatch.setattr(evaluator, "_require_eval_data_clearance", deny_clearance)
    monkeypatch.setattr(handlers, "read_and_extract", forbidden_raw_read)

    with pytest.raises(PermissionError, match="live clearance denied"):
        await handlers.reresolve_collection_relations(
            {"pool": Pool()},
            collection_id=7,
            actor_user_id=11,
        )

    assert raw_read is False


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
    assert len(state["replacements"]) == 1
    assert state["replacements"][0]["parent_chunks"] == []
    assert state["replacements"][0]["leaf_chunks"] == []
    assert result == {"chunk_count": 0, "warning": "no chunks produced"}


async def test_lease_loss_before_chunk_publish_writes_no_index_or_success(
    monkeypatch, tmp_path
):
    class Embedder:
        async def embed(self, _texts, *, user_id=None):
            return [[0.1]]

    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[SimpleNamespace(content="leaf", metadata={})],
        embedder=Embedder(),
    )

    async def claim(*_args, **_kwargs):
        return "lease"

    async def heartbeat_forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    async def lease_lost(*_args, **_kwargs):
        raise handlers.job_state.LeaseLostError("lost")

    monkeypatch.setattr(handlers.job_state, "claim_job", claim)
    monkeypatch.setattr(handlers.job_state, "heartbeat_loop", heartbeat_forever)
    monkeypatch.setattr(handlers.job_state, "ensure_lease", lease_lost)

    with pytest.raises(handlers.job_state.LeaseLostError):
        await handlers.ingest_document(
            state["ctx"], document_id=44, ingestion_job_id=9, attempt_number=1
        )

    assert state["replacements"] == []
    assert not any(
        update.get("status") == "succeeded" for update in state["job_updates"]
    )


async def test_active_generation_publish_passes_exact_claimed_lease_token(
    monkeypatch, tmp_path
):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )

    async def claim(*_args, **_kwargs):
        return "claimed-lease-token"

    async def heartbeat_forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    async def lease_valid(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers.job_state, "claim_job", claim)
    monkeypatch.setattr(handlers.job_state, "heartbeat_loop", heartbeat_forever)
    monkeypatch.setattr(handlers.job_state, "ensure_lease", lease_valid)

    await handlers.ingest_document(
        state["ctx"], document_id=44, ingestion_job_id=9, attempt_number=1
    )
    assert len(state["replacements"]) == 1
    assert state["replacements"][0]["source_ingestion_job_id"] == 9
    assert (
        state["replacements"][0]["source_ingestion_lease_token"]
        == "claimed-lease-token"
    )


async def test_index_timeout_becomes_retryable_durable_failure(monkeypatch, tmp_path):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )

    class SlowStore:
        def __init__(self, _pool, *, collection_id):
            assert collection_id == 7

        async def stage_and_activate_generation(self, **_kwargs):
            await asyncio.sleep(1)

    async def claim(*_args, **_kwargs):
        return "lease"

    async def heartbeat_forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    async def lease_valid(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers, "CollectionScopedPgVectorStore", SlowStore)
    monkeypatch.setattr(handlers.job_state, "claim_job", claim)
    monkeypatch.setattr(handlers.job_state, "heartbeat_loop", heartbeat_forever)
    monkeypatch.setattr(handlers.job_state, "ensure_lease", lease_valid)
    previous = handlers.settings.index_timeout_seconds
    handlers.settings.index_timeout_seconds = 0.01
    try:
        with pytest.raises(StoreError) as exc_info:
            await handlers.ingest_document(
                state["ctx"], document_id=44, ingestion_job_id=9, attempt_number=1
            )
    finally:
        handlers.settings.index_timeout_seconds = previous
    assert exc_info.value.code == "E_INDEX_TIMEOUT"
    assert exc_info.value.retryable is True
    assert len(state["failures"]) == 1
    assert state["failures"][0].code == "E_INDEX_TIMEOUT"


async def test_parse_timeout_becomes_retryable_durable_failure(monkeypatch, tmp_path):
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[],
        embedder=object(),
    )

    async def parse_timeout(*_args, **_kwargs):
        raise handlers.DocumentParseTimeout("too slow")

    async def claim(*_args, **_kwargs):
        return "lease"

    async def heartbeat_forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(handlers, "read_and_extract", parse_timeout)
    monkeypatch.setattr(handlers.job_state, "claim_job", claim)
    monkeypatch.setattr(handlers.job_state, "heartbeat_loop", heartbeat_forever)

    with pytest.raises(StoreError) as exc_info:
        await handlers.ingest_document(
            state["ctx"], document_id=44, ingestion_job_id=9, attempt_number=1
        )
    assert exc_info.value.code == "E_PARSE_TIMEOUT"
    assert exc_info.value.retryable is True
    assert len(state["failures"]) == 1
    assert state["failures"][0].code == "E_PARSE_TIMEOUT"


async def test_ingest_propagates_effective_classification_to_parent_and_leaf_writes(
    monkeypatch,
    tmp_path,
):
    writes: list[tuple[str, Classification]] = []

    class RecordingStore:
        def __init__(self, _pool, *, collection_id: int) -> None:
            assert collection_id == 7

        async def replace_document_chunks(self, **kwargs):
            writes.append(("replace", kwargs["classification_level"]))
            assert [chunk.chunk_key for chunk in kwargs["parent_chunks"]] == [
                "parent-1"
            ]
            assert [chunk.chunk_key for chunk in kwargs["leaf_chunks"]] == [
                "leaf-1"
            ]
            assert kwargs["embeddings"] == [[0.1]]
            return 2

    class Embedder:
        async def embed(self, texts, *, user_id=None):
            assert texts == ["leaf content"]
            assert user_id == 11
            return [[0.1]]

    chunks = [
        SimpleNamespace(
            content="heading",
            chunk_key="parent-1",
            token_count=1,
            metadata={"chunk_type": "heading"},
        ),
        SimpleNamespace(
            content="leaf content",
            chunk_key="leaf-1",
            token_count=2,
            metadata={"chunk_type": "leaf", "parent_chunk_key": "parent-1"},
        ),
    ]
    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=chunks,
        embedder=Embedder(),
        document_classification="機密",
        collection_classification="極機密",
    )
    monkeypatch.setattr(handlers, "CollectionScopedPgVectorStore", RecordingStore)

    result = await handlers.ingest_document(state["ctx"], document_id=44)

    assert writes == [("replace", Classification.SECRET)]
    assert result["parent_count"] == 1
    assert result["leaf_count"] == 1


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


async def test_active_cancellation_schedules_durable_retry_then_reraises(
    monkeypatch, tmp_path
):
    class CancelledEmbedder:
        async def embed(self, _texts, *, user_id=None):
            raise asyncio.CancelledError

    state = _install_ingest_fakes(
        monkeypatch,
        tmp_path,
        chunks=[SimpleNamespace(content="leaf", metadata={})],
        embedder=CancelledEmbedder(),
    )

    async def claim(*_args, **_kwargs):
        return "lease-token"

    async def heartbeat_forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(handlers.job_state, "claim_job", claim)
    monkeypatch.setattr(handlers.job_state, "heartbeat_loop", heartbeat_forever)

    with pytest.raises(asyncio.CancelledError):
        await handlers.ingest_document(
            state["ctx"], document_id=43, ingestion_job_id=9, attempt_number=1
        )

    assert len(state["failures"]) == 1
    assert state["failures"][0].code == "E_WORKER_CANCELLED"
    assert state["failures"][0].retryable is True
    assert not any(status == "failed" for status, _ in state["document_updates"])


async def test_record_failure_rejects_silent_lease_loss(monkeypatch):
    async def rejected(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers.job_state, "fail_or_retry", rejected)
    token = handlers._ACTIVE_JOB.set((7, 8, "lease"))
    try:
        with pytest.raises(handlers.job_state.LeaseLostError):
            await handlers._record_job_failure(
                object(),
                "arq-id",
                StoreError(
                    code="E_INTERNAL",
                    retryable=True,
                    severity="error",
                    user_message="temporary",
                ),
            )
    finally:
        handlers._ACTIVE_JOB.reset(token)


async def test_legacy_terminal_db_write_failure_is_not_swallowed():
    class Pool:
        class Acquire:
            async def __aenter__(self):
                raise RuntimeError("db down")

            async def __aexit__(self, *_args):
                return None

        def acquire(self):
            return self.Acquire()

    with pytest.raises(RuntimeError, match="db down"):
        await handlers._record_job_failure(
            Pool(),
            "arq-id",
            StoreError(
                code="E_INTERNAL",
                retryable=True,
                severity="error",
                user_message="temporary",
            ),
        )
