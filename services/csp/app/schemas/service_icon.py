"""Server-owned icon allow-list for registered services / platform-links.

Governance picker (GET /api/platform-links/icons) and SPA SERVICE_ICONS
keys share this set. Lookups on the SPA must never throw — governance may
add keys here before the SPA ships them.
"""

from __future__ import annotations

# Subset of apps/anila-shell/src/icons.jsx. Keys match the old free-text
# hint so already-stored values (workflow/git/…) keep rendering.
ALLOWED_SERVICE_ICONS: frozenset[str] = frozenset(
    {
        "workflow",
        "git",
        "notebook",
        "chat",
        "monitor",
        "database",
        "api",
        "docs",
        "cpu",
    }
)


def validate_service_icon(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped not in ALLOWED_SERVICE_ICONS:
        raise ValueError(f"未知的圖示 '{stripped}'")
    return stripped
