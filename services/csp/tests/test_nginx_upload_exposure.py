"""Static nginx regression tests for /uploads/ allowlist shape."""

from __future__ import annotations

import re
from pathlib import Path


def _nginx_conf() -> str:
    return (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "nginx"
        / "anila.conf"
    ).read_text(encoding="utf-8")


def _upload_blocks(conf: str) -> list[str]:
    """Return each contiguous /uploads-* location cluster (both TLS listeners)."""
    # Match from ingestion deny through the catch-all return-404 that closes
    # the allowlist (comments outside the locations are excluded).
    pattern = re.compile(
        r"location \^~ /uploads/ingestion/\s*\{[^}]+\}"
        r".*?"
        r"location /uploads/flux/\s*\{[^}]+\}"
        r".*?"
        r"location /uploads/\s*\{\s*return 404;\s*\}",
        re.DOTALL,
    )
    return pattern.findall(conf)


def test_nginx_uploads_allowlist_shape() -> None:
    conf = _nginx_conf()

    # Pin the allowlist SHAPE, not a substring that also matches the deny-all.
    assert "location /uploads/flux/" in conf, "flux alias must exist"
    assert "alias /usr/share/nginx/share-files/uploads/flux/" in conf
    assert re.search(
        r"location /uploads/\s*\{\s*return 404;\s*\}", conf
    ), "location /uploads/ must return 404 (not a public alias)"

    # Must NOT reopen the old public alias of the whole uploads tree.
    assert "alias /usr/share/nginx/share-files/uploads/;" not in conf, (
        "public /uploads/ alias must stay closed"
    )

    # Do not use ^~ on flux — that skips ``location ~ /\\.`` and serves dotfiles.
    assert "location ^~ /uploads/flux/" not in conf, (
        "^~ on /uploads/flux/ would bypass the dotfile deny"
    )

    blocks = _upload_blocks(conf)
    assert len(blocks) == 2, (
        f"both TLS listeners must carry the allowlist pair; found {len(blocks)}"
    )
    assert blocks[0] == blocks[1], (
        "443 and 4443 /uploads/ allowlist blocks must be identical"
    )

    deny = conf.find("location ^~ /uploads/ingestion/")
    flux = conf.find("location /uploads/flux/")
    catch_m = re.search(r"location /uploads/\s*\{\s*return 404;", conf)
    assert deny != -1 and flux != -1 and catch_m is not None
    assert deny < flux < catch_m.start(), (
        "order must be: ingestion deny → flux alias → /uploads/ 404"
    )
