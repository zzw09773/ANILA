"""預設把模型角色解析成固定名稱，讓既有管線測試不必各別模擬 CSP。

真正要打角色端點的測試加 ``@pytest.mark.real_model_roles``。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_platform_roles(monkeypatch, request):
    if request.node.get_closest_marker("real_model_roles"):
        return

    async def _role(role: str) -> str:
        if role == "vision":
            return "vision-llm"
        return "deck-llm"

    monkeypatch.setattr(
        "app.services.studio_model_primary.require_role_model",
        _role,
    )

    async def _no_image_role() -> None:
        return None

    monkeypatch.setattr(
        "app.services.studio_model_primary.resolve_image_generation",
        _no_image_role,
    )
