"""Render an InfographicSpec → chart PNG → HTML → PDF.

Pipeline:

    spec ──► render_chart_png()   ╮  matplotlib (Agg backend)
                                  ▼
    spec ──► render_html()    ◄── PNG bytes base64-inlined
                                  │
                                  ▼
    html ──► render_pdf()      Playwright headless chromium

## Why matplotlib + Agg + Noto Sans CJK TC

1. matplotlib 是 anila-studio pyproject.toml 已經 pin 的 dep。它的 Agg
   backend 不需要 X server，最適合容器 server-side render。
2. **必須在 import pyplot 之前** ``matplotlib.use("Agg")`` ── 否則 process
   可能會綁到 Tk/Qt backend、render 直接 raise。本檔頂部就鎖。
3. CJK 字型：Dockerfile 已裝 ``fonts-noto-cjk``。matplotlib 預設 sans
   字型不含中日韓字符會渲染成「□」豆腐。明示設 ``font.sans-serif`` =
   ``["Noto Sans CJK TC", "sans-serif"]`` 才能保證 chart title / x_labels
   的中文不亂碼。

## 為什麼 PNG base64 inline 進 HTML 而非 file ref

inline 後 HTML 是「一個檔」── 使用者下載後可單檔分享、不用打包資料夾，
也避免 Playwright 在 PDF 轉檔時還要 resolve external resource path。
代價是檔案略大 (~30-100 KB per chart) — 對 infographic 來說可接受。
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any

# IMPORTANT: backend must be set BEFORE pyplot import. Module-level side
# effect — there's no way around this for matplotlib.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (must come after use("Agg"))

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from app.schemas.infographic import (  # noqa: E402
    ChartSpec,
    InfographicSpec,
)

logger = logging.getLogger(__name__)


# ── matplotlib runtime config ──────────────────────────────────────────────

# Noto Sans CJK 在 fonts-noto-cjk 是 .ttc(TrueType Collection)── 內含
# JP / SC / TC / HK / KR 5 個 face。**matplotlib font_manager 對 .ttc 只
# 自動 register 第一個 face**(=JP),所以單純設 rcParams="Noto Sans CJK TC"
# 是找不到的 → fall back DejaVu Sans → 中文豆腐。
#
# 修法兩步:
# 1. 用 font_manager.fontManager.addfont() 把 .ttc 路徑顯式 register,
#    這會把所有 face 都加進來,包含 TC。
# 2. rcParams 列 TC + SC + JP 多個保底:萬一 TC face register 失敗
#    (e.g. local test host 沒 fonts-noto-cjk),退到 JP/SC ── JP face
#    內含 CJK Unified Ideographs 區段,大部分中文字會用日文字型 typography
#    render,雖風格不對但**字看得出來**,比豆腐好。
from matplotlib import font_manager as _fm  # noqa: E402

# Register Noto Sans CJK TC 獨立 single-face OTF(Dockerfile build 階段
# wget 下來)。優先順序:
#   1. /usr/share/fonts/opentype/noto-tc/NotoSansCJKtc-{Regular,Bold}.otf
#      ── Google Noto 官方 TC single-face,matplotlib 對 OTF 認得完整,
#      typography 是真正的台灣字形。
#   2. /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
#      ── debian fonts-noto-cjk 套件,matplotlib 對 .ttc 只 register 首個
#      face(JP)。fallback 保底,中文還能 render 只是 typography 偏日文。
for _candidate in (
    "/usr/share/fonts/opentype/noto-tc/NotoSansCJKtc-Regular.otf",
    "/usr/share/fonts/opentype/noto-tc/NotoSansCJKtc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
):
    try:
        _fm.fontManager.addfont(_candidate)
    except (OSError, FileNotFoundError):
        continue

matplotlib.rcParams["font.sans-serif"] = [
    "Noto Sans CJK TC",   # 真正 TC face(Dockerfile wget 進來的 single-face OTF)
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",   # fallback:.ttc 首個 face,字看得到但日文 typography
    "Noto Sans",
    "sans-serif",
]
matplotlib.rcParams["axes.unicode_minus"] = False

# Quieten matplotlib's "Glyph X missing from font(s) DejaVu Sans"
# UserWarnings on hosts without Noto CJK. Production docker has the
# font; the warning is noise during local pytest runs. Narrowed by
# message regex so unrelated matplotlib warnings still surface.
import warnings as _warnings  # noqa: E402

_warnings.filterwarnings(
    "ignore",
    message=r".*Glyph \d+ \(.*\) missing from font.*",
    category=UserWarning,
)


# 圖表配色：跟 Studio slides 的 corporate_navy 風格對齊（藍系為主，
# 紅/橘做對比色）。維持單一 palette 才能讓多張 chart 在同份 infographic
# 看起來像「一個 deck」而不是拼湊。
_PALETTE = [
    "#1f4e8a",  # navy
    "#3a8bce",  # bright blue
    "#f0a500",  # amber
    "#c0392b",  # red
    "#16a085",  # teal
    "#8e44ad",  # purple
]


# ── Chart rendering ────────────────────────────────────────────────────────


def render_chart_png(spec: ChartSpec) -> bytes:
    """Render a single chart to PNG bytes via matplotlib.

    Dispatch on ``chart_type``: bar / line / pie / donut / hbar.
    series shape: ``[{"name": "...", "values": [n, ...]}]``. For
    pie/donut only series[0] is used (matplotlib pie takes a flat
    array).

    Returns PNG bytes. Always closes the matplotlib figure to avoid
    leaking GPU/font handles across many renders in one process.
    """
    # Use a constrained_layout context to avoid Title clipping while
    # still keeping x-tick labels readable. figsize chosen so the PNG
    # roughly fills half the page width when inline in HTML.
    fig, ax = plt.subplots(figsize=(8, 5), dpi=120)
    try:
        if spec.chart_type == "bar":
            _draw_bar(ax, spec)
        elif spec.chart_type == "hbar":
            _draw_hbar(ax, spec)
        elif spec.chart_type == "line":
            _draw_line(ax, spec)
        elif spec.chart_type == "pie":
            _draw_pie(ax, spec, donut=False)
        elif spec.chart_type == "donut":
            _draw_pie(ax, spec, donut=True)
        else:
            # Pydantic Literal already rejects unknown values at validate
            # time. Keep a defensive fallback so a refactor adding a new
            # chart_type doesn't crash production until the renderer is
            # taught about it — fall back to a bar chart.
            logger.warning(
                "Unknown chart_type %r; falling back to bar.", spec.chart_type
            )
            _draw_bar(ax, spec)

        ax.set_title(spec.title, fontsize=14, fontweight="bold", pad=12)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        return buf.getvalue()
    finally:
        plt.close(fig)


def _series_values(s: dict[str, Any]) -> list[float]:
    """Coerce a series['values'] list to floats, defensively.

    LLM output sometimes ships ints, sometimes strings. matplotlib
    happily plots ints; strings would TypeError. Force-cast here.
    """
    raw = s.get("values") or []
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _draw_bar(ax: Any, spec: ChartSpec) -> None:
    n_series = len(spec.series)
    n_x = len(spec.x_labels)
    width = 0.8 / max(1, n_series)
    for i, s in enumerate(spec.series):
        vals = _series_values(s)
        # Pad / truncate so len(vals) matches n_x.
        vals = (vals + [0.0] * n_x)[:n_x]
        positions = [j + (i - (n_series - 1) / 2) * width for j in range(n_x)]
        ax.bar(
            positions,
            vals,
            width=width,
            color=_PALETTE[i % len(_PALETTE)],
            label=str(s.get("name", f"series {i + 1}")),
        )
    ax.set_xticks(range(n_x))
    ax.set_xticklabels(spec.x_labels, rotation=20, ha="right")
    if n_series > 1:
        ax.legend(loc="best")
    ax.grid(axis="y", linestyle=":", alpha=0.4)


def _draw_hbar(ax: Any, spec: ChartSpec) -> None:
    # Horizontal bar: x_labels become y-axis category labels.
    n_x = len(spec.x_labels)
    n_series = len(spec.series)
    height = 0.8 / max(1, n_series)
    for i, s in enumerate(spec.series):
        vals = _series_values(s)
        vals = (vals + [0.0] * n_x)[:n_x]
        positions = [j + (i - (n_series - 1) / 2) * height for j in range(n_x)]
        ax.barh(
            positions,
            vals,
            height=height,
            color=_PALETTE[i % len(_PALETTE)],
            label=str(s.get("name", f"series {i + 1}")),
        )
    ax.set_yticks(range(n_x))
    ax.set_yticklabels(spec.x_labels)
    if n_series > 1:
        ax.legend(loc="best")
    ax.grid(axis="x", linestyle=":", alpha=0.4)


def _draw_line(ax: Any, spec: ChartSpec) -> None:
    n_x = len(spec.x_labels)
    for i, s in enumerate(spec.series):
        vals = _series_values(s)
        vals = (vals + [0.0] * n_x)[:n_x]
        ax.plot(
            range(n_x),
            vals,
            marker="o",
            color=_PALETTE[i % len(_PALETTE)],
            label=str(s.get("name", f"series {i + 1}")),
            linewidth=2.0,
        )
    ax.set_xticks(range(n_x))
    ax.set_xticklabels(spec.x_labels, rotation=20, ha="right")
    if len(spec.series) > 1:
        ax.legend(loc="best")
    ax.grid(linestyle=":", alpha=0.4)


def _draw_pie(ax: Any, spec: ChartSpec, *, donut: bool) -> None:
    # Only first series renders for pie/donut — pie is a 1-D chart.
    first = spec.series[0] if spec.series else {"values": []}
    vals = _series_values(first)
    # Drop labels whose value is 0 or negative — matplotlib would
    # draw zero-width wedges and clutter the legend.
    labels = list(spec.x_labels)
    pairs = [(lbl, v) for lbl, v in zip(labels, vals) if v > 0]
    if not pairs:
        # Defensive: no positive values → fake a single 100% slice so
        # the chart still renders (alternative is a blank fig).
        pairs = [("(empty)", 1.0)]
    labels, vals = zip(*pairs)
    colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(labels))]
    wedge_kwargs: dict[str, Any] = {"colors": colors, "startangle": 90}
    if donut:
        wedge_kwargs["wedgeprops"] = {"width": 0.4}
    ax.pie(
        vals,
        labels=labels,
        autopct="%1.0f%%",
        **wedge_kwargs,
    )
    ax.set_aspect("equal")


# ── HTML rendering ─────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Build a Jinja2 env pointed at app/templates/infographic/.

    Cached at module level via lru_cache? Not strictly necessary —
    FileSystemLoader is cheap, and we only render one infographic per
    request. Keep simple, easier to reason about.
    """
    templates_dir = Path(__file__).resolve().parent.parent / "templates" / "infographic"
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        # Force autoescape ON for every template — our templates have the
        # ``.html.j2`` extension which ``select_autoescape(['html','xml'])``
        # does NOT match (it checks ``.html`` / ``.xml`` exactly). Explicit
        # ``autoescape=True`` defends against XSS from LLM-controlled
        # string fields (title, takeaway, etc).
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(spec: InfographicSpec, chart_pngs: dict[int, bytes]) -> str:
    """Render the infographic to a single self-contained HTML string.

    ``chart_pngs`` is a dict keyed by chart index → raw PNG bytes;
    they get base64-encoded into ``<img src="data:image/png;...">``
    tags inline. No external assets — the resulting HTML is one file.

    The base template uses CSS grid for the stats row, table for the
    comparison matrix, and a flex column for the timeline. Each
    preset is just a different ``preset_class`` on body — actual
    section visibility is driven by spec data presence, not by preset.
    """
    encoded_charts: dict[int, str] = {
        idx: base64.b64encode(png).decode("ascii") for idx, png in chart_pngs.items()
    }

    env = _jinja_env()
    template = env.get_template("base.html.j2")
    html = template.render(
        spec=spec,
        encoded_charts=encoded_charts,
        preset_class=f"preset-{spec.preset.value}",
    )
    return html


# ── PDF rendering ──────────────────────────────────────────────────────────


async def render_pdf(html: str, dest_path: Path) -> None:
    """Convert ``html`` to PDF at ``dest_path`` via Playwright chromium.

    Same pattern the slide pipeline's QA screenshot path uses — open
    a headless chromium, set content, ``page.pdf(...)``. ``page.pdf``
    only works in headless chromium (not firefox/webkit), so we
    explicitly launch chromium.

    Caller is responsible for ensuring the parent dir exists. Raises
    whatever Playwright raises on launch failure; the job runner has
    the surrounding try/except.
    """
    # Local import so module import doesn't pull playwright (and its
    # transitive deps) into processes that only need chart rendering
    # — e.g. unit tests that mock out the PDF step.
    from playwright.async_api import async_playwright

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            await page.pdf(
                path=str(dest_path),
                format="A4",
                # Margins kept small — the HTML template owns its own
                # outer padding so we don't double up.
                margin={
                    "top": "10mm",
                    "bottom": "10mm",
                    "left": "10mm",
                    "right": "10mm",
                },
                print_background=True,
            )
        finally:
            await browser.close()
