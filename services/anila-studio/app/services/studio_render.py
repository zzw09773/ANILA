"""把簡報規格變成 pptx。

知識庫裡已有的圖、Graphviz 圖表，以及（生圖角色健康時）經 CSP 產生的配圖，
都在 ``_hydrate_images`` 收成 ``image_data``。沒有圖的 image_focus 會改回
一般版面，不留空框。
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import HTTPException

from app.clients.csp_client import (
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    fetch_image_blob,
)
from app.services.studio_config import (
    CONTENT_ILLUSTRATION_MAX_BULLETS,
    MAX_GENERATED_IMAGES_PER_DECK,
    RENDERER_BASE_URL,
)

if TYPE_CHECKING:
    from app.schemas.studio import ImageUseCase, SlidesSpec, StyleDescriptor
    from app.services.studio_llm import StudioLLMAdapter as _StudioLLMAdapter

logger = logging.getLogger(__name__)


def _infer_image_use_case(idx: int, slide: dict) -> "ImageUseCase":
    """Map a slide to its FLUX use_case. Order matters: idx 0 / layout 'cover'
    is the hero even when also tagged section_break."""
    from app.schemas.studio import ImageUseCase

    if idx == 0 or slide.get("layout_kind") == "cover":
        return ImageUseCase.COVER_HERO
    if slide.get("layout_kind") == "section_break":
        return ImageUseCase.SECTION_BAND
    return ImageUseCase.CONTENT_ILLUSTRATION


def _drop_empty_image_frame(slide: dict) -> None:
    """拿掉生成圖的請求，並把沒有圖的 image_focus 改回一般版面。

    不寫佔位文字。知識庫圖與圖表走別的路徑，不會進這裡。
    """
    slide.pop("image_prompt", None)
    if slide.get("image_kind") == "illustration":
        slide.pop("image_kind", None)
    if slide.get("layout_kind") == "image_focus" and not slide.get("image_data"):
        slide["layout_kind"] = "standard"


def _apply_illustration_fallback(slide: dict, use_case: "ImageUseCase") -> None:
    """No usable image for this slide: drop image fields so the renderer
    degrades (theme cover / theme section break / text-only standard layout),
    and record the fallback in image_gen_meta. Never sets image_data.

    This is the single fallback writer — the generation helper returns False
    without writing meta, and the routing calls this for both gate-failure and
    the per-deck cap.
    """
    from app.schemas.studio import ImageUseCase

    label = {
        ImageUseCase.COVER_HERO: "solid_theme_cover",
        ImageUseCase.SECTION_BAND: "theme_section_break",
        ImageUseCase.CONTENT_ILLUSTRATION: "text_only",
    }[use_case]
    meta = slide.get("image_gen_meta") or {}
    meta.setdefault("use_case", use_case.value)
    meta["fallback"] = label
    slide["image_gen_meta"] = meta
    slide.pop("image_data", None)
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)


async def _hydrate_images(
    spec_dict: dict[str, Any],
    images_lookup: dict[str, dict[str, Any]],
    *,
    bearer: str,
    flux_provider: Any = None,
    default_aspect: str = "16:9",
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """把 image_ref、圖表、以及生成配圖收成 inline PNG。

    每張的順序：知識庫原圖 > Graphviz 圖表 > 經 CSP 的生圖。
    生圖角色沒設、不健康，或請求失敗時，拿掉配圖框，不留佔位文字。
    ``flux_provider`` 只是舊呼叫點留下的參數，不再使用。
    """
    del flux_provider, deck_base_seed, llm, deck_style
    import base64

    from app.clients.csp_client import proxy_image_generation
    from app.schemas.studio import ImageUseCase
    from app.services.diagram_renderer import render_dot_to_png
    from app.services.studio_model_primary import resolve_image_generation

    image_model = await resolve_image_generation()
    slides = spec_dict.get("slides") or []
    generated_count = 0
    for idx, slide in enumerate(slides):
        # Path 1: image_ref → resolve via csp_client.fetch_image_blob
        ref = slide.get("image_ref")
        if ref:
            meta = images_lookup.get(ref)
            if not meta:
                slide.pop("image_ref", None)
                # If a fallback image_prompt is present, try that next
            else:
                try:
                    blob, fetched_mime = await fetch_image_blob(
                        int(meta["image_id"]), bearer=bearer,
                    )
                    mime = meta.get("mime") or fetched_mime or "image/png"
                    slide["image_data"] = (
                        f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
                    )
                    # ref wins over both generative paths
                    slide.pop("image_prompt", None)
                    slide.pop("diagram_dot", None)
                    slide.pop("image_kind", None)
                    continue
                except (CspNotFoundError, CspForbiddenError) as e:
                    # csp lost the row (storage purged) or the bearer no
                    # longer has access — surface the same warning pattern
                    # the old OSError branch used and degrade to the next
                    # image-resolution path.
                    logger.warning(
                        "Failed to hydrate image_ref=%s via csp blob fetch: %s — "
                        "falling back to diagram_dot / image_prompt if available.",
                        ref, e,
                    )
                    slide.pop("image_ref", None)
                except (CspServerError, CspClientError) as e:
                    # Transient csp outage — log and fall through so the
                    # deck still renders with whatever fallback path the
                    # slide has next.
                    logger.warning(
                        "csp blob fetch failed for image_ref=%s: %s — "
                        "falling back to diagram_dot / image_prompt if available.",
                        ref, e,
                    )
                    slide.pop("image_ref", None)

        # Path 2: diagram_dot → render via Graphviz
        # Studio Fix 2 (2026-05-18): deterministic labelled diagrams.
        # Wins over image_prompt because FLUX can't render text legibly.
        dot = slide.get("diagram_dot")
        if dot and slide.get("image_kind") == "diagram":
            png_bytes = await render_dot_to_png(dot)
            if png_bytes is not None:
                slide["image_data"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(png_bytes).decode("ascii")
                )
                # Diagram succeeded; drop any leftover prompt fields.
                slide.pop("image_prompt", None)
                slide.pop("diagram_dot", None)
                slide.pop("image_kind", None)
                continue
            # Render failed — drop the diagram fields so the renderer's
            # image_focus → standard fallback kicks in. (image_prompt is
            # intentionally NOT tried here: the LLM decided this was a
            # diagram, not an illustration; falling through to FLUX
            # would put garbled-text output back on the slide.)
            logger.warning(
                "Studio diagram path: graphviz render returned None for slide '%s' "
                "(dot binary missing? CJK font missing? syntax error?). Slide will "
                "fall back to standard layout. Run scripts/diagnose-graphviz.sh "
                "(Round 2 Patch G runbook) to identify root cause.",
                slide.get("title", "<untitled>"),
            )
            slide.pop("diagram_dot", None)
            slide.pop("image_kind", None)
            continue

        # 生成圖只在生圖角色已設定且健康時，經 CSP 代理請求。
        # 沒設或失敗就拿掉配圖框，不留佔位文字。
        wants_generated = (
            slide.get("image_kind") == "illustration"
            or bool(slide.get("image_prompt"))
        )
        if wants_generated:
            use_case = _infer_image_use_case(idx, slide)
            too_dense = (
                use_case is ImageUseCase.CONTENT_ILLUSTRATION
                and len(slide.get("bullets") or []) > CONTENT_ILLUSTRATION_MAX_BULLETS
            )
            prompt = str(slide.get("image_prompt") or "").strip()
            if (
                image_model
                and prompt
                and not too_dense
                and generated_count < MAX_GENERATED_IMAGES_PER_DECK
            ):
                size = (
                    "1792x1024"
                    if use_case is not ImageUseCase.CONTENT_ILLUSTRATION
                    else "1024x1024"
                )
                generated = await proxy_image_generation(
                    model=image_model,
                    prompt=prompt,
                    size=size,
                    bearer=bearer,
                    task_id=task_id,
                )
                if generated:
                    raw, mime = generated
                    generated_count += 1
                    slide["image_data"] = (
                        f"data:{mime};base64,"
                        + base64.b64encode(raw).decode("ascii")
                    )
                    slide.pop("image_prompt", None)
                    slide.pop("image_kind", None)
                    if use_case is ImageUseCase.CONTENT_ILLUSTRATION:
                        slide["layout_kind"] = "image_focus"
                    continue
            _drop_empty_image_frame(slide)
            continue
        if slide.get("layout_kind") == "image_focus" and not slide.get("image_data"):
            slide["layout_kind"] = "standard"

    return spec_dict


class RenderOutput(tuple):
    """``(pptx_bytes, pptx_path)`` that still unpacks as a 2-tuple, plus the
    QA alignment facts the renderer reports in headers:

    * ``kinds`` — rendered kind per slide in deck order (``cover`` first when
      the renderer prepended its own title slide);
    * ``cover_prepended`` — whether rendered index 0 is that synthetic cover.

    Kept as a tuple subclass so every existing ``bytes, path = await
    _render_pptx(...)`` call (and test fake) keeps working unchanged.
    """

    kinds: list[str]
    cover_prepended: bool

    def __new__(cls, pptx_bytes: bytes, pptx_path: str, *, kinds: list[str] | None = None, cover_prepended: bool = False):
        self = super().__new__(cls, (pptx_bytes, pptx_path))
        self.kinds = list(kinds or [])
        self.cover_prepended = bool(cover_prepended)
        return self


async def _render_pptx(
    spec: SlidesSpec,
    images_lookup: dict[str, dict[str, Any]] | None = None,
    *,
    bearer: str,
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
    task_id: str | None = None,
) -> "RenderOutput":
    """POST spec → renderer → (pptx bytes, server-side path).

    Returns the path so /screenshots can refer to it without us having to
    base64 the .pptx through CSP memory.

    送去渲染前先補上知識庫原圖。生圖角色健康時才向 CSP 要配圖；
    否則拿掉空的配圖框。
    """
    del deck_base_seed, llm, deck_style
    spec_dict = await _hydrate_images(
        spec.model_dump(),
        images_lookup or {},
        bearer=bearer,
        default_aspect="16:9",
        task_id=task_id,
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                f"{RENDERER_BASE_URL}/render",
                json={"spec": spec_dict},
            )
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503,
                detail=f"pptx-renderer unreachable: {e}",
            ) from e
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"pptx-renderer /render returned {r.status_code}: {r.text[:300]}",
        )
    pptx_path = r.headers.get("X-Pptx-Path", "")
    kinds_header = r.headers.get("X-Pptx-Slide-Kinds", "")
    kinds = [k for k in kinds_header.split(",") if k] if kinds_header else []
    cover_prepended = r.headers.get("X-Pptx-Cover-Prepended", "0") == "1"
    return RenderOutput(r.content, pptx_path, kinds=kinds, cover_prepended=cover_prepended)
