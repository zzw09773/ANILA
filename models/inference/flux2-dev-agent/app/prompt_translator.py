"""Rewrite the user's natural-language description into a FLUX-friendly
English prompt by delegating to ``gemma4`` through the CSP proxy.

FLUX.2-dev has decent Chinese support (its text encoder is
Mistral-Small-24B which is multilingual), but conversational Chinese
("我想看那個...部隊在山上那種感覺") still benefits from being
rewritten into the descriptive English style FLUX is mostly trained
on. When ``enabled=False`` (e.g. for offline tests or as a kill
switch), this class is a pure pass-through.

On any error from gemma4 we fall back to the original text rather
than failing the whole request — partial degradation is better than
no image at all.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You rewrite a user's casual image-generation request into a single "
    "concise English prompt suitable for the FLUX.2-dev text-to-image "
    "model. Preserve all concrete subjects, settings, props, and mood. "
    "Add brief style hints (composition, lighting, photographic vs. "
    "illustrated) only when they are clearly implied. Reply with ONLY "
    "the rewritten prompt — no quotes, no commentary, no leading 'Prompt:'."
)


class PromptTranslator:
    def __init__(
        self,
        *,
        csp_base_url: str,
        csp_api_key: str,
        gemma_model: str,
        enabled: bool,
        timeout: float = 15.0,
    ) -> None:
        self._csp_base_url = csp_base_url.rstrip("/")
        self._csp_api_key = csp_api_key
        self._gemma_model = gemma_model
        self._enabled = enabled
        self._timeout = timeout

    async def translate(self, user_text: str) -> str:
        if not self._enabled:
            return user_text

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._csp_base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._csp_api_key}"},
                    json={
                        "model": self._gemma_model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_text},
                        ],
                        "stream": False,
                        "temperature": 0.2,
                    },
                )
            if resp.status_code != 200:
                logger.warning(
                    "prompt translation failed (status=%s); falling back to original",
                    resp.status_code,
                )
                return user_text

            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content or user_text
        except Exception:
            logger.exception("prompt translation errored; falling back to original")
            return user_text
