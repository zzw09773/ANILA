"""Join registered OpenAI-compatible endpoint addresses with versioned paths.

Registry rows store either a bare host (``http://h:1``) or a versioned base
(``http://h:1/v1`` / ``/v2``). Call sites historically guessed which convention
was canonical and produced ``/v1/v1/...`` (or ``/v2/v1/...``) upstream 404s.

``model_registry.api_version`` (or the version embedded in the caller-supplied
``endpoint_path``) is the single source of truth for which version to request.
A version segment sitting on the stored address is legacy noise — normalise it
away rather than letting two sources disagree.

This helper is *not* idempotent under ``join(join(b, p), p)``: the second call
sees a base whose final segment is a resource name (e.g. ``completions``), not
an API version, so it appends the path again. No call site double-applies.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_VERSIONS = ("v1", "v2")


def join_upstream_path(base_url: str, path: str) -> str:
    """Join a registered endpoint address with an upstream path.

    ``path`` is typically version-qualified (``/v1/chat/completions``,
    ``/v1/embeddings``, ``/v1/models``) or a non-version probe (``/health``,
    ``/``).

    When ``path`` begins with ``/v1`` or ``/v2``, any trailing API version
    segment on ``base_url`` is stripped first, then the path is appended.
    The path's version therefore always wins (``base …/v2`` + ``/v1/...`` →
    ``…/v1/...``; ``base …/v1`` + ``/v2/embeddings`` → ``…/v2/embeddings``).

    Never emits a duplicated ``/v1/v1/`` (or ``/v2/v1/``) segment or a double
    slash. A real sub-path on the base (e.g. ``http://h:1/openai``) is
    preserved. A version in the *middle* of the base path
    (``http://h/v1/proxy``) is not a trailing segment and is preserved.
    Non-version probe paths are appended as-is without stripping the base.
    """
    base = (base_url or "").rstrip("/")
    if not path:
        return base
    if not path.startswith("/"):
        path = f"/{path}"

    version, rest = _split_version_path(path)
    if version is None:
        if path == "/":
            return f"{base}/"
        return f"{base}{path}"

    base = strip_trailing_api_version(base)
    return f"{base}/{version}{rest}"


def strip_trailing_api_version(base_url: str) -> str:
    """Remove a final ``/v1`` or ``/v2`` path segment if present.

    Used by ``join_upstream_path`` (and the ``api_version=="v2"`` embedding
    special case) so a registered ``…/v1`` or ``…/v2`` address does not stack
    a second version segment when the caller asks for a (possibly different)
    version in the path.
    """
    base = (base_url or "").rstrip("/")
    parts = urlsplit(base)
    segs = [s for s in parts.path.split("/") if s]
    if not segs or segs[-1] not in _VERSIONS:
        return base
    new_path = "/" + "/".join(segs[:-1]) if len(segs) > 1 else ""
    return urlunsplit(
        (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
    ).rstrip("/")


def _split_version_path(path: str) -> tuple[str | None, str]:
    """Return ``(version, rest)`` when ``path`` begins with ``/v1`` or ``/v2``.

    ``rest`` keeps a leading slash (e.g. ``/chat/completions``) or is empty
    when the path is exactly the version segment.
    """
    for version in _VERSIONS:
        exact = f"/{version}"
        prefix = f"/{version}/"
        if path == exact:
            return version, ""
        if path.startswith(prefix):
            return version, path[len(exact) :]
    return None, path
