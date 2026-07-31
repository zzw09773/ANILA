#!/usr/bin/env python3
"""設計 token 對比色檢查 — WCAG AA（手動、非 CI）。

掃 apps/anila-shell、apps/anilalm、apps/csp-governance-ui 的 token 定義，
在 light／dark 兩套主題下，對「文字色 × 背景色」常見配對算對比，報 < 4.5:1
（一般文字 AA）的組合。純標準庫，無新依賴。

限制（本腳本會印在輸出開頭，請當面讀）
------------------------------------
靜態分析**抓不到**合成疊加：浮水印、半透明 overlay、漸層上的字、
canvas／SVG 文字、執行期改色。今晚浮水印在深色背景被淹沒、淺色卻正常——
那種問題只能靠渲染目視。本檢查只覆蓋「token 表裡寫死的成對色」。
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_FINDINGS = 3

AA_NORMAL = 4.5

# 每個 app：如何取 light/dark token，以及要檢查的 (fg, bg) 配對名。
SHELL_HTML = REPO / "apps" / "anila-shell" / "index.html"
GOV_CSS = REPO / "apps" / "csp-governance-ui" / "src" / "assets" / "styles" / "tokens.css"
ANILALM_TS = REPO / "apps" / "anilalm" / "src" / "theme" / "tokens.ts"

HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
RGB_RE = re.compile(
    r"rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)(?:\s*,\s*([0-9.]+))?\s*\)"
)
OKLCH_RE = re.compile(
    r"oklch\(\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)(?:\s*\/\s*([0-9.]+%?))?\s*\)"
)
CSS_VAR_RE = re.compile(
    r"(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);"
)


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 8:
        h = h[:6]  # drop alpha for contrast of opaque text assumption
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r / 255.0, g / 255.0, b / 255.0


def _oklch_to_rgb(L: float, C: float, H: float) -> tuple[float, float, float]:
    """近似 OKLCH→sRGB（無第三方庫；夠用來抓明顯不合格）。"""
    # OKLab → linear sRGB（Björn Ottosson）
    h_rad = math.radians(H)
    a = C * math.cos(h_rad)
    b = C * math.sin(h_rad)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b2 = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def enc(x: float) -> float:
        x = max(0.0, min(1.0, x))
        return 12.92 * x if x <= 0.0031308 else 1.055 * (x ** (1 / 2.4)) - 0.055

    return enc(r), enc(g), enc(b2)


def parse_color(value: str) -> tuple[float, float, float] | None:
    value = value.strip()
    if value.startswith("var("):
        return None
    m = HEX_RE.search(value)
    if m:
        return _hex_to_rgb(m.group(0))
    m = RGB_RE.search(value)
    if m:
        r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
        if r > 1 or g > 1 or b > 1:
            r, g, b = r / 255.0, g / 255.0, b / 255.0
        return r, g, b
    m = OKLCH_RE.search(value)
    if m:
        return _oklch_to_rgb(float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return None


def rel_luminance(rgb: tuple[float, float, float]) -> float:
    def f(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = f(rgb[0]), f(rgb[1]), f(rgb[2])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    L1, L2 = rel_luminance(fg), rel_luminance(bg)
    lighter, darker = max(L1, L2), min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)


def parse_css_themes(text: str) -> dict[str, dict[str, str]]:
    """切 :root / [data-theme=light] / [data-theme=dark] 區塊的 CSS 變數。

    丟掉 ``@media (prefers-color-scheme: …)`` 整段——那是 OS 偏好覆寫,
    會把深色 token 誤灌進 light 的 ``:root``。
    """
    text = re.sub(
        r"@media\s*\([^)]*prefers-color-scheme[^)]*\)\s*\{(?:[^{}]|\{[^{}]*\})*\}",
        "",
        text,
        flags=re.DOTALL,
    )
    themes: dict[str, dict[str, str]] = {"light": {}, "dark": {}}
    for m in re.finditer(r"([^{}/]+)\{([^{}]*)\}", text, flags=re.DOTALL):
        sel, body = m.group(1).strip(), m.group(2)
        vars_ = {k: v.strip() for k, v in CSS_VAR_RE.findall(body)}
        if not vars_:
            continue
        sel_l = re.sub(r"\s+", " ", sel)
        if "data-theme=\"dark\"" in sel_l or "data-theme='dark'" in sel_l:
            themes["dark"].update(vars_)
        elif "data-theme=\"light\"" in sel_l or "data-theme='light'" in sel_l:
            themes["light"].update(vars_)
        elif re.search(r"(^|[, ]):root\b", sel_l) and "data-theme" not in sel_l:
            # 裸 :root = 預設淺色（殼層／治理 UI 皆如此）
            themes["light"].update(vars_)
    return themes


def parse_anilalm_tokens(text: str) -> dict[str, dict[str, str]]:
    themes: dict[str, dict[str, str]] = {"dark": {}, "light": {}}
    for theme in ("dark", "light"):
        m = re.search(
            rf"{theme}\s*:\s*\{{(.*?)\n\s*\}},",
            text,
            flags=re.DOTALL,
        )
        if not m:
            continue
        body = m.group(1)
        for km in re.finditer(
            r"(\w+)\s*:\s*['\"]([^'\"]+)['\"]", body
        ):
            themes[theme][km.group(1)] = km.group(2)
    return themes


def check_pairs(
    app: str,
    theme: str,
    tokens: dict[str, str],
    pairs: list[tuple[str, str]],
) -> list[str]:
    out: list[str] = []
    for fg_name, bg_name in pairs:
        if fg_name not in tokens or bg_name not in tokens:
            continue
        fg = parse_color(tokens[fg_name])
        bg = parse_color(tokens[bg_name])
        if fg is None or bg is None:
            continue
        ratio = contrast_ratio(fg, bg)
        if ratio < AA_NORMAL:
            out.append(
                f"{app}/{theme}: {fg_name} on {bg_name} = {ratio:.2f}:1 (< {AA_NORMAL})"
            )
    return out


def main() -> int:
    print(
        "LIMIT: 靜態檢查抓不到浮水印／半透明疊加／漸層上的字／執行期改色；"
        "那些只能渲染目視。以下只報 token 表內的文字×背景配對。"
    )
    findings: list[str] = []

    try:
        # —— anila-shell（oklch in index.html）——
        shell_text = SHELL_HTML.read_text(encoding="utf-8")
        shell_themes = parse_css_themes(shell_text)
        shell_pairs = [
            ("--fg", "--bg"),
            ("--fg", "--bg-elev"),
            ("--fg", "--bg-subtle"),
            ("--fg-muted", "--bg"),
            ("--fg-muted", "--bg-elev"),
            ("--fg-muted", "--bg-subtle"),
            ("--fg-subtle", "--bg"),
            ("--fg-subtle", "--bg-elev"),
            ("--fg-subtle", "--bg-subtle"),
            ("--accent-fg", "--accent"),
            ("--accent", "--bg"),
            ("--danger", "--bg"),
            ("--warn", "--bg"),
            ("--success", "--bg"),
        ]
        # dark block 不重設 accent 等 → 從 light 繼承未覆寫的
        base_light = dict(shell_themes.get("light") or {})
        dark = dict(base_light)
        dark.update(shell_themes.get("dark") or {})
        for theme_name, toks in (("light", base_light), ("dark", dark)):
            findings.extend(check_pairs("anila-shell", theme_name, toks, shell_pairs))

        # —— governance-ui ——
        gov_text = GOV_CSS.read_text(encoding="utf-8")
        gov_themes = parse_css_themes(gov_text)
        gov_pairs = [
            ("--c-fg-1", "--c-bg"),
            ("--c-fg-1", "--c-surface-1"),
            ("--c-fg-1", "--c-surface-2"),
            ("--c-fg-2", "--c-bg"),
            ("--c-fg-2", "--c-surface-1"),
            ("--c-fg-3", "--c-bg"),
            ("--c-fg-3", "--c-surface-1"),
            ("--c-fg-mute", "--c-bg"),
            ("--c-fg-mute", "--c-surface-1"),
            ("--c-accent-fg", "--c-accent"),
            ("--c-accent", "--c-bg"),
            ("--c-danger", "--c-bg"),
            ("--c-warn", "--c-bg"),
            ("--c-ok", "--c-bg"),
            ("--c-info", "--c-bg"),
        ]
        for theme_name in ("light", "dark"):
            findings.extend(
                check_pairs(
                    "csp-governance-ui",
                    theme_name,
                    gov_themes.get(theme_name) or {},
                    gov_pairs,
                )
            )

        # —— anilalm（TS tokens）——
        alm_text = ANILALM_TS.read_text(encoding="utf-8")
        alm_themes = parse_anilalm_tokens(alm_text)
        alm_pairs = [
            ("text", "bg"),
            ("text", "surface"),
            ("text", "surface2"),
            ("text", "elevated"),
            ("textMuted", "bg"),
            ("textMuted", "surface"),
            ("textSubtle", "bg"),
            ("textSubtle", "surface"),
            ("accent", "bg"),
            ("danger", "bg"),
            ("warning", "bg"),
            ("success", "bg"),
        ]
        for theme_name in ("light", "dark"):
            findings.extend(
                check_pairs(
                    "anilalm", theme_name, alm_themes.get(theme_name) or {}, alm_pairs
                )
            )
    except Exception as exc:
        print(f"BROKEN: {exc}", file=sys.stderr)
        return EXIT_BROKEN

    if findings:
        print(f"\n對比不足 WCAG AA（{AA_NORMAL}:1）— {len(findings)} 組:")
        for line in findings:
            print(f"  ✖ {line}")
        return EXIT_FINDINGS

    print("contrast: token 成對檢查通過（兩主題）。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
