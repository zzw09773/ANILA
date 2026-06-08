"""Studio render + FLUX illustration pipeline (Step 7 of the flowchart).

Extracted from ``api/studio.py`` (god-module split). Everything that turns a
validated ``SlidesSpec`` into .pptx bytes lives here:

  * ``get_flux_provider`` — lazily-built FluxImageProvider singleton.
  * ``_gated_generate`` — Stage 2 candidate-generate + quality-gate retry loop.
  * ``_infer_image_use_case`` / ``_apply_illustration_fallback`` — per-slide
    image routing + the single fallback writer.
  * ``_generate_slide_illustration`` — rewrite → gate → write one slide image.
  * ``_hydrate_images`` — resolve every slide's image_ref / diagram_dot /
    illustration into inline base64 PNG (curated > deterministic > generative).
  * ``_render_pptx`` — POST the hydrated spec to the Node pptx-renderer.

The quality-gate VLM (``_Gemma4VlmGate``) is imported from ``studio_llm`` and
held as a module global here so ``_generate_slide_illustration`` resolves it in
*this* module's namespace — tests monkeypatch it (and the helpers above) on
``app.services.studio_render``. ``_StudioLLMAdapter`` is only a type annotation
here (it is built in studio's ``_run_pipeline`` and passed in), so it is a
TYPE_CHECKING import.
"""

from __future__ import annotations

import base64
import logging
import os
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
    FLUX_GATE_MAX_RETRIES,
    FLUX_GATE_NUM_CANDIDATES,
    FLUX_GATE_SEED_STRIDE,
    MAX_GENERATED_IMAGES_PER_DECK,
    RENDERER_BASE_URL,
)
from app.services.studio_llm import Gemma4VlmGate as _Gemma4VlmGate

if TYPE_CHECKING:
    from app.schemas.studio import ImageUseCase, SlidesSpec, StyleDescriptor
    from app.services.flux_image_provider import FluxImageProvider, GeneratedImage
    from app.services.studio_llm import StudioLLMAdapter as _StudioLLMAdapter

logger = logging.getLogger(__name__)


# Module-level singleton: built once on import (or on first call), held
# until process exit. Shared semaphore inside ensures concurrency
# limit holds across all slide-generation requests.
_FLUX_PROVIDER: "FluxImageProvider | None" = None
_FLUX_PROVIDER_INITIALISED = False


def get_flux_provider() -> "FluxImageProvider | None":
    """Return the configured FluxImageProvider, or None if FLUX
    integration is disabled in this deployment.

    Configuration via env:
      FLUX_BACKEND_URL       (required to enable; e.g. http://flux2-dev:8000)
      FLUX_CACHE_DIR         (default: /var/anila/anila-studio-flux-cache)
      FLUX_MAX_CONCURRENT    (default: 4)
      FLUX_TIMEOUT_SECONDS   (default: 180)
    """
    global _FLUX_PROVIDER, _FLUX_PROVIDER_INITIALISED
    if _FLUX_PROVIDER_INITIALISED:
        return _FLUX_PROVIDER

    flux_url = os.environ.get("FLUX_BACKEND_URL", "").strip()
    if not flux_url:
        _FLUX_PROVIDER_INITIALISED = True
        return None

    from app.services.flux_image_provider import FluxImageProvider

    # anila-studio 自己的 cache volume — 不借 csp 的 INGESTION_UPLOAD_DIR
    # (那是 csp 內 ingestion 上傳目錄,anila-studio container 沒掛/沒權限)。
    cache_dir = os.environ.get(
        "FLUX_CACHE_DIR", "/var/anila/anila-studio-flux-cache"
    )
    max_concurrent = int(os.environ.get("FLUX_MAX_CONCURRENT", "4"))
    timeout = float(os.environ.get("FLUX_TIMEOUT_SECONDS", "180"))

    from pathlib import Path
    _FLUX_PROVIDER = FluxImageProvider(
        flux_url=flux_url,
        cache_dir=Path(cache_dir),
        max_concurrent=max_concurrent,
        timeout_seconds=timeout,
    )
    _FLUX_PROVIDER_INITIALISED = True
    logger.info(
        "FluxImageProvider wired: url=%s cache_dir=%s concurrent=%d",
        flux_url, cache_dir, max_concurrent,
    )
    return _FLUX_PROVIDER


async def _gated_generate(
    flux_provider: "FluxImageProvider",
    prompt: str,
    *,
    use_case: "ImageUseCase",
    seed: int,
    style_id: str,
    concept_en: str,
    vlm: "_Gemma4VlmGate",
) -> tuple["GeneratedImage | None", int]:
    """Stage 2 retry loop (spec 5.2): generate N candidates, run the quality
    gate, retry with a fresh seed on total failure.

    Returns ``(accepted_image_or_None, retry_count)``. ``retry_count`` is
    the number of EXTRA attempts beyond the first (0 = accepted first try).

    A cache hit short-circuits the whole loop: a previously gate-accepted
    image for this (prompt, use_case, seed, style) tuple is returned as-is
    so a job re-run is deterministic and free.
    """
    from app.services.flux_quality_gate import gate_candidates

    cached = flux_provider.cached_image(
        prompt, use_case=use_case, seed=seed, style_id=style_id,
    )
    if cached is not None:
        return cached, 0

    for attempt in range(FLUX_GATE_MAX_RETRIES + 1):
        attempt_seed = seed + attempt * FLUX_GATE_SEED_STRIDE
        candidates = await flux_provider.generate_candidates(
            prompt,
            use_case=use_case,
            seed=attempt_seed,
            num_candidates=FLUX_GATE_NUM_CANDIDATES,
        )
        best = await gate_candidates(
            candidates,
            concept_en=concept_en,
            vlm=vlm,
        )
        if best is not None:
            # Persist under the per-slide (deterministic) seed, not the
            # per-attempt retry seed, so a re-run hits the cache.
            flux_provider.persist_chosen(
                best, prompt=prompt, use_case=use_case, seed=seed, style_id=style_id,
            )
            logger.info(
                "[gate] accepted on attempt %d/%d (use_case=%s)",
                attempt, FLUX_GATE_MAX_RETRIES, use_case.value,
            )
            return best, attempt
        logger.info(
            "[gate] attempt %d/%d rejected all %d candidates, retrying fresh seed",
            attempt, FLUX_GATE_MAX_RETRIES, FLUX_GATE_NUM_CANDIDATES,
        )
    logger.warning(
        "[gate] all %d attempts exhausted (use_case=%s) — fallback to no image",
        FLUX_GATE_MAX_RETRIES + 1, use_case.value,
    )
    return None, FLUX_GATE_MAX_RETRIES


def _infer_image_use_case(idx: int, slide: dict) -> "ImageUseCase":
    """Map a slide to its FLUX use_case. Order matters: idx 0 / layout 'cover'
    is the hero even when also tagged section_break."""
    from app.schemas.studio import ImageUseCase

    if idx == 0 or slide.get("layout_kind") == "cover":
        return ImageUseCase.COVER_HERO
    if slide.get("layout_kind") == "section_break":
        return ImageUseCase.SECTION_BAND
    return ImageUseCase.CONTENT_ILLUSTRATION


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


async def _generate_slide_illustration(
    slide: dict,
    *,
    idx: int,
    use_case: "ImageUseCase",
    deck_style: "StyleDescriptor | None",
    flux_provider: "FluxImageProvider",
    deck_base_seed: int,
    llm: "_StudioLLMAdapter",
) -> bool:
    """Rewrite → gate → write image. Returns True iff an image was placed.

    On any failure (rewriter raised / USE_GRAPHVIZ / gate raised / all
    candidates rejected) returns False WITHOUT writing fallback meta — the
    caller invokes _apply_illustration_fallback. ``concept_en`` keeps the
    Stage 1 convention (the whole flux_prompt); the slide's own image_prompt
    is intentionally ignored (decision A) — title+bullets drive the rewriter.
    """
    from app.services.flux_prompt_rewriter import derive_flux_prompt
    from app.services.flux_style import get_style_descriptor

    style = deck_style or get_style_descriptor()
    title = slide.get("title", "")
    try:
        flux_prompt = await derive_flux_prompt(
            title=title,
            bullets=slide.get("bullets", []),
            use_case=use_case,
            style=style,
            llm=llm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX rewriter failed for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if not flux_prompt:  # USE_GRAPHVIZ or stripped empty
        logger.info(
            "FLUX rewriter returned no prompt for slide '%s' (%s) — fallback.",
            title or "<untitled>", use_case.value,
        )
        return False

    seed = deck_base_seed + idx
    vlm = _Gemma4VlmGate(llm._bearer)
    try:
        best, retry_count = await _gated_generate(
            flux_provider,
            flux_prompt,
            use_case=use_case,
            seed=seed,
            style_id=style.style_id,
            concept_en=flux_prompt,
            vlm=vlm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX gated generation errored for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if best is None:
        logger.warning(
            "FLUX gate rejected all candidates for slide '%s' (%s) after %d "
            "retries — fallback.", title or "<untitled>", use_case.value, retry_count,
        )
        return False

    slide["image_data"] = (
        "data:image/png;base64," + base64.b64encode(best.png_bytes).decode("ascii")
    )
    slide["image_gen_meta"] = {
        "use_case": use_case.value,
        "flux_prompt": flux_prompt,
        "seed": best.seed,
        "style_id": style.style_id,
        "clip_score": best.clip_score,
        "vlm_verdict": best.vlm_verdict,
        "retry_count": retry_count,
    }
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)
    return True


async def _hydrate_images(
    spec_dict: dict[str, Any],
    images_lookup: dict[str, dict[str, Any]],
    *,
    bearer: str,
    flux_provider: "FluxImageProvider | None" = None,
    default_aspect: str = "16:9",
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
) -> dict[str, Any]:
    """Resolve every Slide.image_ref / diagram_dot / image_prompt into inline base64 PNG.

    Order of precedence per slide (curated > deterministic > generative):
      1. image_ref present and resolvable → inline existing PNG (fetched
         from csp's ``/api/ingestion/images/{id}/blob`` endpoint, so we
         no longer need a shared ``upload_dir`` mount).
      2. image_ref present but unresolvable → drop, fall back to next path.
      3. image_kind='diagram' + diagram_dot → render via Graphviz `dot -Tpng`.
      4. diagram render fails → drop diagram_dot/image_kind, fall back to next path.
      4b. FLUX Stage 1 cover hero: when this slide is the cover (index 0 or
          layout_kind=="cover") and the rewriter + flux_provider + seed are
          available, derive a house-styled FLUX prompt from title+bullets
          and generate a 16:9 hero. This sits ABOVE the legacy image_prompt
          path so a cover gets a deterministic, rewriter-controlled image
          rather than whatever raw prompt the LLM may have stuffed in.
      5. image_prompt present and flux_provider available → generate via FLUX.
      6. image_prompt present but flux_provider None or FLUX fails → drop, standard layout.
      7. Nothing set → leave untouched.

    Failure modes mirror each other: drop the offending field, log warning,
    let the renderer's image_focus → standard fallback take over. The
    diagram path exists because FLUX.2-dev (diffusion) cannot render
    legible text — labelled diagrams (architecture, flow, ER) get crisp
    output via Graphviz instead.

    `deck_base_seed` + `llm` enable the Stage 1 cover-hero path; when either
    is None the function behaves exactly as before (the legacy image_ref /
    diagram / image_prompt paths only). ``bearer`` is required even when
    no ``image_ref`` slides exist because the FLUX rewriter / VLM gate
    eventually flow through csp's proxy as well.
    """
    import base64

    from app.schemas.studio import ImageUseCase
    from app.services.diagram_renderer import render_dot_to_png

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

        # Path 4: FLUX illustration — cover hero / section band / content,
        # all through the same rewriter + deck_style + quality gate. Replaces
        # the Stage 1 cover-only block and the legacy image_prompt Path 3.
        wants_illustration = (
            idx == 0
            or slide.get("layout_kind") in ("cover", "section_break")
            or slide.get("image_kind") == "illustration"
            or bool(slide.get("image_prompt"))
        )
        toolchain_ready = (
            flux_provider is not None
            and deck_base_seed is not None
            and llm is not None
        )
        if wants_illustration and toolchain_ready:
            # Order matters: compute use_case BEFORE the cap check (the cap
            # fallback needs it).
            use_case = _infer_image_use_case(idx, slide)
            # Density gate: the renderer only shows a content illustration on an
            # image_focus (image-led) layout. A bullet-heavy content slide would
            # be crowded by that, so it keeps its text-only standard layout and
            # skips generation entirely (no GPU spent, doesn't count toward cap).
            if (
                use_case is ImageUseCase.CONTENT_ILLUSTRATION
                and len(slide.get("bullets") or []) > CONTENT_ILLUSTRATION_MAX_BULLETS
            ):
                _apply_illustration_fallback(slide, use_case)
                continue
            if generated_count >= MAX_GENERATED_IMAGES_PER_DECK:
                logger.warning(
                    "per-deck image cap %d reached; slide %d (%s) -> fallback",
                    MAX_GENERATED_IMAGES_PER_DECK, idx, use_case.value,
                )
                _apply_illustration_fallback(slide, use_case)
                continue
            generated_count += 1
            ok = await _generate_slide_illustration(
                slide,
                idx=idx,
                use_case=use_case,
                deck_style=deck_style,
                flux_provider=flux_provider,
                deck_base_seed=deck_base_seed,
                llm=llm,
            )
            if ok:
                # Content illustration only renders on an image_focus layout;
                # cover/section bands render full-bleed via renderSectionBreak
                # and must keep their layout.
                if use_case is ImageUseCase.CONTENT_ILLUSTRATION:
                    slide["layout_kind"] = "image_focus"
            else:
                _apply_illustration_fallback(slide, use_case)
            continue
        if wants_illustration and slide.get("image_prompt"):
            # FLUX toolchain not wired for this deployment but a legacy prompt
            # is present: drop it so the renderer doesn't act on an unused field.
            slide.pop("image_prompt", None)
            slide.pop("image_kind", None)

    return spec_dict


async def _render_pptx(
    spec: SlidesSpec,
    images_lookup: dict[str, dict[str, Any]] | None = None,
    *,
    bearer: str,
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
) -> tuple[bytes, str]:
    """POST spec → renderer → (pptx bytes, server-side path).

    Returns the path so /screenshots can refer to it without us having to
    base64 the .pptx through CSP memory.

    `images_lookup` (optional) is the dict that drove the LLM's
    image-suggestion list, keyed by image_id. When provided, every
    Slide.image_ref gets hydrated into inline `image_data` bytes via
    `_hydrate_images` before the spec leaves the anila-studio boundary.
    Hydration now pulls bytes from csp via ``fetch_image_blob`` rather
    than a shared filesystem mount, so ``bearer`` is required.

    `deck_base_seed` + `llm` (FLUX Stage 1) enable the cover-hero generation
    path inside `_hydrate_images`. They are threaded from the job pipeline
    (deck_base_seed = sha256(job_id)).
    """
    spec_dict = spec.model_dump()
    flux_provider = get_flux_provider()
    # Hydrate when there are curated images to resolve OR when the FLUX
    # cover-hero path is fully wired (provider + seed + llm). The latter
    # matters for text-only knowledge bases: no retrieved images means an
    # empty images_lookup, but a cover hero should still be generated.
    cover_hero_ready = (
        flux_provider is not None and deck_base_seed is not None and llm is not None
    )
    if images_lookup or cover_hero_ready:
        spec_dict = await _hydrate_images(
            spec_dict,
            images_lookup or {},
            bearer=bearer,
            flux_provider=flux_provider,
            default_aspect="16:9",
            deck_base_seed=deck_base_seed,
            llm=llm,
            deck_style=deck_style,
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
    return r.content, pptx_path
