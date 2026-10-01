# -*- coding: utf-8 -*-
"""治理中心前端的 SPA fallback。

nginx ``location /api/`` 是 ``proxy_pass`` 到 CSP，沒有 ``try_files``。
沒有對應路由的 ``/api/*``（例如 ``GET /api/health``）以前掉進
``/{full_path:path}``，回 200 的 ``index.html``。那條改回 JSON 404。
其餘路徑仍是前端路由。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_NOT_FOUND = {"detail": "Not Found"}
_API_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def is_unmatched_api_path(full_path: str) -> bool:
    path = (full_path or "").lstrip("/")
    return path == "api" or path.startswith("api/")


def api_not_found_response() -> JSONResponse:
    return JSONResponse(status_code=404, content=dict(_NOT_FOUND))


def install_unmatched_api_404(app: FastAPI) -> None:
    """掛在既有 /api 路由之後、SPA catch-all 之前。先註冊的路由先匹配。"""

    async def unmatched_api() -> JSONResponse:
        return api_not_found_response()

    async def unmatched_api_rest(rest: str) -> JSONResponse:
        del rest
        return api_not_found_response()

    for path, name in (("/api", "unmatched_api_exact"), ("/api/", "unmatched_api_slash")):
        app.add_api_route(
            path,
            unmatched_api,
            methods=_API_METHODS,
            include_in_schema=False,
            name=name,
        )
    app.add_api_route(
        "/api/{rest:path}",
        unmatched_api_rest,
        methods=_API_METHODS,
        include_in_schema=False,
        name="unmatched_api_rest",
    )


def _path_has_dot_segment(full_path: str) -> bool:
    """任何一段以 "." 開頭（.env、.git、目錄裡的隱藏檔）都不當靜態檔。"""
    for segment in (full_path or "").replace("\\", "/").split("/"):
        if segment.startswith("."):
            return True
    return False


class FrontendAssetFiles(StaticFiles):
    """/assets 掛載。StaticFiles 會擋目錄穿越，但不會拒絕 dotfile。"""

    async def get_response(self, path: str, scope):
        if _path_has_dot_segment(path):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


def spa_response(full_path: str, frontend_root: Path):
    """未命中的 /api/* 是 JSON 404。其他路徑沿用 SPA，且不走出前端目錄。"""
    root = frontend_root.resolve()
    if is_unmatched_api_path(full_path):
        return api_not_found_response()
    index_path = root / "index.html"
    # 任何含 NUL / 非法 byte 的 path → 直接給 index.html。
    # 點開頭的路徑段也不讀檔，避免把 dist 裡的 .env 或 .git 送出去。
    if "\x00" in full_path or _path_has_dot_segment(full_path):
        return FileResponse(str(index_path))
    try:
        candidate = (root / full_path).resolve()
    except (OSError, ValueError):
        return FileResponse(str(index_path))
    # 必須仍位於前端目錄子樹中；否則視為 SPA route fallback。
    try:
        candidate.relative_to(root)
    except ValueError:
        return FileResponse(str(index_path))
    if candidate.is_file():
        return FileResponse(str(candidate))
    return FileResponse(str(index_path))


def register_frontend_spa(app: FastAPI, frontend_root: Path) -> None:
    root = frontend_root.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        return spa_response(full_path, root)
