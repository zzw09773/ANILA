"""Per-model context window table — drives auto-compact thresholds.

The framework is local-first (vLLM / NIM / TGI / Ollama). Common
self-hosted models have wildly different context windows (Gemma-2 8K,
Llama-3.1 128K, Qwen2.5 128K, Llama-3 8K, …) and the auto-compact
math needs to know the right ceiling per model.

Operators populate the table at deployment with the actual values
configured on their inference server. Defaults below are reference
sizes for popular local models — override them when your deployment
runs the model with a different ``--max-model-len``.

Lookup semantics mirror the framework's PriceTable: exact match
first, then longest-prefix fallback. Unknown model → 8K (the most
conservative reasonable floor) rather than crashing — operators see
extra-aggressive compaction and notice via dashboards rather than
having a session OOM.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


_DEFAULT_FALLBACK_WINDOW = 8_192
"""Used when a model isn't in the table. Conservative — picks
the smallest common local-model window so unconfigured deployments
fail safe (over-compact, not OOM)."""


# Reference local-model windows. Operators override at startup with
# the actual --max-model-len they pass to vLLM / NIM / TGI / Ollama.
DEFAULT_LOCAL_MODEL_WINDOWS: dict[str, int] = {
    # Google Gemma family
    "google/gemma4": 8_192,
    "google/gemma-2": 8_192,
    "google/gemma-3": 128_000,
    # Meta Llama
    "meta-llama/Llama-3-8B": 8_192,
    "meta-llama/Llama-3.1": 128_000,
    "meta-llama/Llama-3.2": 128_000,
    "meta/llama-4-maverick": 1_000_000,
    # Qwen
    "Qwen/Qwen2.5": 128_000,
    "Qwen/Qwen3": 128_000,
    # Mistral
    "mistralai/Mistral-7B": 32_768,
    "mistralai/Mixtral-8x7B": 32_768,
    # NVIDIA NIM bundles (very deployment-specific; operator should override)
    "nvidia/llama-3.1-nemotron": 128_000,
}


class ModelWindowTable:
    """Lookup model name → max context window tokens.

    Construction with no args uses ``DEFAULT_LOCAL_MODEL_WINDOWS``;
    pass an explicit dict to override (recommended for production).
    Mutable: ``add()`` / ``update()`` for runtime adjustments.
    """

    def __init__(
        self,
        windows: dict[str, int] | None = None,
        *,
        fallback: int = _DEFAULT_FALLBACK_WINDOW,
    ) -> None:
        self._exact: dict[str, int] = (
            dict(windows) if windows is not None else dict(DEFAULT_LOCAL_MODEL_WINDOWS)
        )
        self._fallback = fallback

    def add(self, model: str, window: int) -> None:
        if window <= 0:
            raise ValueError(f"window must be positive, got {window}")
        self._exact[model] = window

    def update(self, windows: dict[str, int]) -> None:
        for model, window in windows.items():
            self.add(model, window)

    def get(self, model: str) -> int:
        """Return the configured window for ``model`` or the fallback.

        Lookup order:
          1. Exact match
          2. Longest prefix match (``meta-llama/Llama-3.1-8B-Instruct``
             matches ``meta-llama/Llama-3.1`` if present)
          3. Fallback constant (8K) with a one-time DEBUG log so
             operators can spot the missing entry in the logs.
        """
        if model in self._exact:
            return self._exact[model]
        for key in sorted(self._exact, key=len, reverse=True):
            if model.startswith(key):
                return self._exact[key]
        logger.debug(
            "ModelWindowTable: no entry for %r, using fallback %d tokens",
            model,
            self._fallback,
        )
        return self._fallback

    def __contains__(self, model: object) -> bool:
        if not isinstance(model, str):
            return False
        if model in self._exact:
            return True
        return any(model.startswith(k) for k in self._exact)


__all__ = ["DEFAULT_LOCAL_MODEL_WINDOWS", "ModelWindowTable"]
