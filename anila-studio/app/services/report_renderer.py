"""Render a ``ReportSpec`` into HTML / PDF / DOCX artifacts on disk.

## Pipeline

    ReportSpec
        │
        │  (1) Jinja2 → HTML string
        │      • markdown_it_py converts each section's
        │        content_markdown → inline HTML
        │      • per-preset template inherits base.html.j2
        │
        ├─→ HTML file (UTF-8, self-contained — no external CSS/JS)
        │
        ├─→ (2) Playwright headless chromium
        │      • set_content(html) then page.pdf(...)
        │      → PDF file
        │
        └─→ (3) pypandoc.convert_text(html, 'docx', ...)
               → DOCX file

## Why Playwright instead of weasyprint?

The base template uses ``@page`` and CSS sizing that weasyprint supports,
but Playwright + Chromium handles CJK fonts (Noto Sans CJK TC) more
reliably out of the box. The renderer is run inside a container that
already has the playwright chromium browser pre-installed (per the
anila-studio Dockerfile — Phase 0 added the ``playwright install
chromium`` step).

## Why pypandoc for DOCX?

pandoc has the best HTML→DOCX converter in the Python ecosystem. The
input is our own well-formed HTML, so we avoid the well-known pandoc
quirks around raw HTML strings.

## Async + threading

- Playwright exposes async-native APIs; we use them directly.
- pypandoc is sync. Running it inline would block the event loop while
  pandoc shells out. We use ``asyncio.to_thread`` to offload.

## Errors

All three render paths can fail (chromium crash, pandoc not installed,
disk full). The runner catches any exception and surfaces it as
``state="failed"`` with the truncated message; the renderer itself does
not swallow errors.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
from markdown_it import MarkdownIt

from app.schemas.report import ReportPreset, ReportSection, ReportSpec

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────


# Map preset enum → Jinja2 template filename. The dispatch table also lets
# us validate the request at render-time even if the schema enum drifts
# from the templates directory.
_PRESET_TEMPLATE: dict[ReportPreset, str] = {
    ReportPreset.DEEP_TECH_REVIEW: "report/deep_tech_review.html.j2",
    ReportPreset.KEY_SUMMARY: "report/key_summary.html.j2",
    ReportPreset.TEACHING_HANDOUT: "report/teaching_handout.html.j2",
    ReportPreset.EXTERNAL_COMMS: "report/external_comms.html.j2",
}

# Human-readable preset label used in the document footer.
_PRESET_LABEL: dict[ReportPreset, str] = {
    ReportPreset.DEEP_TECH_REVIEW: "深度技術綜述",
    ReportPreset.KEY_SUMMARY: "重點摘要",
    ReportPreset.TEACHING_HANDOUT: "教學講義",
    ReportPreset.EXTERNAL_COMMS: "對外溝通文件",
}


# ── Section-with-html projection ─────────────────────────────────────────


@dataclass(frozen=True)
class _SectionWithHTML:
    """Section view with content_markdown pre-rendered to HTML.

    The Jinja2 template's ``render_section`` macro receives this so it
    can use ``content_html`` directly without invoking a Jinja2 filter at
    each iteration (filters that build their own MarkdownIt instances
    blow the per-render budget).
    """

    heading: str
    content_html: str
    content_markdown: str  # kept for templates that still want the raw
    subsections: list["_SectionWithHTML"]


def _new_markdown() -> MarkdownIt:
    """One MarkdownIt instance per render — they're cheap to construct,
    and giving each render its own ensures plugins don't leak state.

    We DISABLE the html option so any raw HTML the LLM emits in
    content_markdown (e.g. ``<script>``, ``<iframe>``) is rendered as
    escaped text rather than passed through as markup. This is defence
    in depth: although the chain of CSP + Jinja autoescape + sandbox
    should already prevent script execution in the produced PDF/DOCX
    workflow, surfacing literal angle brackets is safer than relying on
    downstream pipelines.
    """
    return MarkdownIt("commonmark", {"html": False})


def _render_section_html(section: ReportSection, md: MarkdownIt) -> _SectionWithHTML:
    """Recursively render a section's markdown to HTML.

    Citation markers like ``[3]`` are kept verbatim — the LLM emits them
    as plain text and the renderer's CSS does NOT auto-link them today
    (linking would require regex'ing inside the rendered HTML and risk
    eating array indices like ``data[3]``). Future enhancement: an explicit
    ``{ref:3}`` syntax that the markdown plugin layer handles.
    """
    content_html = md.render(section.content_markdown or "")
    subs = [_render_section_html(s, md) for s in section.subsections]
    return _SectionWithHTML(
        heading=section.heading,
        content_html=content_html,
        content_markdown=section.content_markdown,
        subsections=subs,
    )


# ── Jinja2 environment ────────────────────────────────────────────────────


def _build_env() -> jinja2.Environment:
    """Construct a Jinja2 environment for the report templates.

    Loader: package-relative ``app/templates/``. Autoescape: enabled for
    .html/.j2 files. We disable autoescape inside the macro body by using
    ``|safe`` on the pre-rendered ``content_html`` (markdown_it already
    escaped untrusted HTML during the markdown render).
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            searchpath=str(Path(__file__).parent.parent / "templates")
        ),
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html", "html.j2", "j2")
        ),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )


# Memoise the env — file loader path scan is cheap but constructing on
# every render adds noticeable overhead under load and provides no benefit.
_env: jinja2.Environment | None = None


def _get_env() -> jinja2.Environment:
    global _env
    if _env is None:
        _env = _build_env()
    return _env


# ── HTML render ───────────────────────────────────────────────────────────


def render_html(spec: ReportSpec) -> str:
    """Render a ReportSpec to a complete HTML5 document string.

    Pure function. Safe to call from any thread (Jinja2 environments are
    thread-safe for rendering; only construction is not).

    Raises ``KeyError`` if the preset isn't in ``_PRESET_TEMPLATE`` (would
    indicate a schema/renderer drift — fail fast so we never silently
    fall back to a wrong layout).
    """
    template_name = _PRESET_TEMPLATE[spec.preset]
    env = _get_env()
    template = env.get_template(template_name)

    md = _new_markdown()
    sections_with_html = [_render_section_html(s, md) for s in spec.sections]

    # Render generated_at in a local-friendly format; not in the template
    # because Jinja2 escaping of the ISO string is fine but the dash style
    # we want (YYYY-MM-DD HH:MM UTC) is nicer in Python.
    generated_at_str = spec.generated_at.strftime("%Y-%m-%d %H:%M UTC")

    return template.render(
        spec=spec,
        sections_with_html=sections_with_html,
        preset_label=_PRESET_LABEL[spec.preset],
        generated_at_str=generated_at_str,
    )


def write_html_file(spec: ReportSpec, dest: Path) -> None:
    """Render and write the HTML file to disk (UTF-8).

    Sync helper — wrap with ``asyncio.to_thread`` if invoked from an
    async context where blocking on disk write is a concern.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(spec)
    dest.write_text(html, encoding="utf-8")


# ── PDF render (Playwright) ───────────────────────────────────────────────


async def write_pdf_file(html: str, dest: Path) -> None:
    """Convert an HTML string to a PDF file via headless chromium.

    Launches a fresh browser per render. For Phase 1 throughput
    (≤1 report per user-minute) this is fine; if the deck-rendering load
    pattern grows, we can swap to a persistent browser pool but that
    adds lifecycle complexity that's not justified yet.

    Margins / page size are CSS-driven (``@page`` rules in
    ``base.html.j2``). We still pass ``format="A4"`` and explicit margins
    here so the print output is deterministic even if a future template
    omits the ``@page`` block.

    Raises whatever Playwright raises on chromium failure; the runner
    layer maps that to ``state="failed"``.
    """
    # Import lazily so test environments without chromium installed can
    # still import this module (tests mock async_playwright entirely).
    from playwright.async_api import async_playwright

    dest.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await page.pdf(
                path=str(dest),
                format="A4",
                margin={
                    "top": "18mm",
                    "right": "16mm",
                    "bottom": "18mm",
                    "left": "16mm",
                },
                print_background=True,
            )
        finally:
            await browser.close()


# ── DOCX render (pypandoc) ────────────────────────────────────────────────


def _convert_docx_sync(html: str, dest: Path) -> None:
    """Sync helper that runs pandoc HTML→DOCX conversion.

    Separated so ``asyncio.to_thread`` can target a plain function (no
    coroutine wrapping). Caller owns directory creation.
    """
    # Lazy import — pypandoc complains about missing pandoc binary at
    # import time only on first call, not import. Tests mock this whole
    # function anyway.
    import pypandoc

    pypandoc.convert_text(
        source=html,
        to="docx",
        format="html",
        outputfile=str(dest),
    )


async def write_docx_file(html: str, dest: Path) -> None:
    """Convert HTML to DOCX via pypandoc, offloaded to a thread.

    pypandoc shells out to the ``pandoc`` binary, which is blocking IO;
    running inline would stall the event loop for hundreds of ms per
    call. ``asyncio.to_thread`` keeps the runner cooperative.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_convert_docx_sync, html, dest)


# ── Combined render entry point ──────────────────────────────────────────


async def render_all_artifacts(
    spec: ReportSpec, artifacts_dir: Path, job_id: str
) -> dict[str, Path]:
    """Render HTML + PDF + DOCX for a spec; return their paths.

    File layout:
      ``{artifacts_dir}/{job_id}.html``
      ``{artifacts_dir}/{job_id}.pdf``
      ``{artifacts_dir}/{job_id}.docx``

    HTML is rendered first (sync, cheap); PDF and DOCX both consume that
    HTML string. They could in principle run in parallel via
    ``asyncio.gather`` — we deliberately keep them sequential for the
    first cut because chromium startup + pandoc invocation both peak
    memory; serialising is the conservative choice for shared infra.

    Returns ``{"html": Path, "pdf": Path, "docx": Path}``.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    html_path = artifacts_dir / f"{job_id}.html"
    pdf_path = artifacts_dir / f"{job_id}.pdf"
    docx_path = artifacts_dir / f"{job_id}.docx"

    # HTML — render once, keep in memory for PDF+DOCX so we don't re-parse.
    html_str = render_html(spec)
    html_path.write_text(html_str, encoding="utf-8")
    logger.info("report renderer: wrote HTML to %s", html_path)

    await write_pdf_file(html_str, pdf_path)
    logger.info("report renderer: wrote PDF to %s", pdf_path)

    await write_docx_file(html_str, docx_path)
    logger.info("report renderer: wrote DOCX to %s", docx_path)

    return {"html": html_path, "pdf": pdf_path, "docx": docx_path}
