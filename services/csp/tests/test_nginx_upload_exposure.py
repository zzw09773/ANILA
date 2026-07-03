"""Static nginx regression tests for private ingestion upload paths."""

from __future__ import annotations

from pathlib import Path


def test_nginx_denies_ingestion_uploads_before_public_upload_alias() -> None:
    conf = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "nginx"
        / "anila.conf"
    ).read_text(encoding="utf-8")

    deny = conf.find("location ^~ /uploads/ingestion/")
    public = conf.find("location /uploads/")
    assert deny != -1, "nginx must deny /uploads/ingestion/ explicitly"
    assert public != -1, "expected public /uploads/ alias"
    assert deny < public, "private ingestion deny must precede public uploads alias"
