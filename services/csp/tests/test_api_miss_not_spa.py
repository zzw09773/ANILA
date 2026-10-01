# -*- coding: utf-8 -*-
"""未命中的 /api/* 是 JSON 404，不是治理中心的 index.html。

HTML 來自 CSP 的 SPA catch-all，不是 nginx ``try_files``。
``location /api/`` 只 ``proxy_pass`` 到 CSP。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.spa_fallback import (
    install_unmatched_api_404,
    register_frontend_spa,
    spa_response,
)

_CONF = Path(__file__).resolve().parents[3] / "infra" / "nginx" / "anila.conf"
_API_COMMENT = (
    "未命中的 /api/* 由 CSP 回 JSON 404。不要在這個 location 用 try_files 回 SPA。"
)
_NOT_FOUND = {"detail": "Not Found"}


def _dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "api").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    # 舊的 catch-all 看到檔案就回檔。這個誘餌必須不能被當成 /api/health。
    (root / "api" / "health").write_text("DECOY-API-HEALTH", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET-OUTSIDE", encoding="utf-8")
    return root


def _assert_json_404(response) -> None:
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == _NOT_FOUND
    assert response.text == '{"detail":"Not Found"}'
    assert "DECOY-API-HEALTH" not in response.text
    assert "<!doctype" not in response.text.lower()


def test_spa_catch_all_does_not_serve_an_api_file(tmp_path):
    """只掛 SPA 時，GET /api/health 仍是 JSON 404，不回誘餌檔或 index。"""
    root = _dist(tmp_path)
    app = FastAPI()
    register_frontend_spa(app, root)
    client = TestClient(app)

    _assert_json_404(client.get("/api/health"))
    page = client.get("/login")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<title>spa</title>" in page.text


def test_unmatched_api_methods_are_json_404_and_real_routes_stay(tmp_path):
    root = _dist(tmp_path)
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.get("/api/banners/public")
    def banners():
        return [{"level": "info", "content": "hi"}]

    install_unmatched_api_404(app)
    register_frontend_spa(app, root)
    client = TestClient(app)

    _assert_json_404(client.get("/api/health"))
    _assert_json_404(client.post("/api/health"))
    _assert_json_404(client.put("/api/health"))
    head = client.head("/api/health")
    assert head.status_code == 404
    assert head.headers["content-type"].startswith("application/json")

    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/api/banners/public").json() == [
        {"level": "info", "content": "hi"}
    ]
    page = client.get("/login")
    assert page.status_code == 200
    assert "<title>spa</title>" in page.text
    asset = client.get("/api/health")
    assert "DECOY-API-HEALTH" not in asset.text


def test_spa_fallback_does_not_serve_dotfiles(tmp_path):
    root = _dist(tmp_path)
    (root / ".env").write_text("SECRET-DOTENV", encoding="utf-8")
    hidden = root / "assets"
    hidden.mkdir(exist_ok=True)
    (hidden / ".map").write_text("SECRET-MAP", encoding="utf-8")
    nested = root / "pkg" / ".hidden"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("SECRET-NESTED", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")

    app = FastAPI()
    register_frontend_spa(app, root)
    client = TestClient(app)

    env = client.get("/.env")
    assert env.status_code == 200
    assert "SECRET-DOTENV" not in env.text
    assert "<title>spa</title>" in env.text

    mapped = client.get("/assets/.map")
    assert "SECRET-MAP" not in mapped.text
    assert "<title>spa</title>" in mapped.text

    deep = client.get("/pkg/.hidden/payload.txt")
    assert "SECRET-NESTED" not in deep.text
    assert "<title>spa</title>" in deep.text

    script = client.get("/app.js")
    assert script.status_code == 200
    assert "console.log(1)" in script.text


def test_spa_response_stays_inside_the_frontend_directory(tmp_path):
    root = _dist(tmp_path)
    escaped = spa_response("../secret.txt", root)
    assert Path(escaped.path).resolve() == (root / "index.html").resolve()
    missed = spa_response("api/health", root)
    assert missed.status_code == 404
    assert missed.body == b'{"detail":"Not Found"}'


def test_asset_mount_rejects_dot_segments_before_static_files(tmp_path):
    """/assets 的 StaticFiles 比 SPA 早掛上，點開頭的路徑段仍不能讀到檔。"""
    from app.spa_fallback import FrontendAssetFiles

    assets = tmp_path / "assets"
    hidden = assets / "pkg" / ".hidden"
    hidden.mkdir(parents=True)
    (assets / ".env").write_text("SECRET-ASSET-ENV", encoding="utf-8")
    (hidden / "payload.txt").write_text("SECRET-ASSET-NESTED", encoding="utf-8")
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    (assets / "chunk.abc123.js").write_text("console.log(2)", encoding="utf-8")

    app = FastAPI()
    app.mount("/assets", FrontendAssetFiles(directory=str(assets)), name="frontend-assets")
    client = TestClient(app)

    env = client.get("/assets/.env")
    assert env.status_code == 404
    assert "SECRET-ASSET-ENV" not in env.text

    deep = client.get("/assets/pkg/.hidden/payload.txt")
    assert deep.status_code == 404
    assert "SECRET-ASSET-NESTED" not in deep.text

    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "console.log(1)" in script.text
    hashed = client.get("/assets/chunk.abc123.js")
    assert hashed.status_code == 200
    assert "console.log(2)" in hashed.text


def test_main_mounts_frontend_assets_with_the_dotfile_guard():
    text = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert "FrontendAssetFiles(directory=str(assets_dir))" in text
    mount_at = text.index("FrontendAssetFiles(directory=str(assets_dir))")
    spa_at = text.index("register_frontend_spa(app, frontend_dist)")
    assert mount_at < spa_at


def test_main_installs_the_api_404_before_the_spa_catch_all():
    text = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    install_at = text.index("install_unmatched_api_404(app)")
    register_at = text.index("register_frontend_spa(app, frontend_dist)")
    assert install_at < register_at


def _location_blocks(text: str, header: str) -> list[str]:
    blocks: list[str] = []
    start = 0
    while True:
        at = text.find(header, start)
        if at < 0:
            return blocks
        brace = text.find("{", at)
        depth = 0
        for index in range(brace, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[at : index + 1])
                    start = index + 1
                    break
        else:
            raise AssertionError(f"unclosed block at {header!r}")


def test_nginx_api_locations_proxy_to_csp_and_do_not_try_files():
    text = _CONF.read_text(encoding="utf-8")
    blocks = _location_blocks(text, "location /api/ {")
    assert len(blocks) == 2
    assert text.count(_API_COMMENT) == 2
    for block in blocks:
        assert _API_COMMENT in block
        assert "proxy_pass http://csp_backend;" in block
        directives = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(line.startswith("try_files") for line in directives)
        assert not any("index.html" in line for line in directives)
